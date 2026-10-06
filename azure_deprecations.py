"""
azure_deprecations.py
=====================
Recursos deprecados (apartado 1.6) — capa de DATOS REALES.

Consulta Azure Advisor (Microsoft.Advisor/recommendations) a traves del Azure
Resource Manager usando la credencial del login, y filtra las recomendaciones
que anuncian retiro / deprecacion de servicios, SKUs o versiones. No usa datos
simulados: todo proviene de los anuncios que Microsoft publica via Advisor.

Para cada recomendacion de retiro se extrae:
    - servicio afectado (categoria / tipo de recurso)
    - recurso o SKU afectado
    - fecha de retiro (si Advisor la expone en extendedProperties)
    - estado (Activo / En retiro / Retirado) derivado de la fecha
    - accion recomendada (solution)

Multi-suscripcion EN PARALELO (I/O de red). Tolerante a errores por sub.
Reutiliza el cliente ARM cacheado de azure_alerts para no recrear pipelines.
"""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import pandas as pd

# Reutiliza helpers del modulo de alertas (cliente ARM cacheado, parsers).
import azure_alerts as _az

try:
    from azure.core.exceptions import HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_ADVISOR_API = "2023-01-01"
# Concurrencia moderada: Advisor devuelve 500 si se le satura con demasiadas
# llamadas simultaneas. 4 es un buen balance entre velocidad y estabilidad.
_MAX_WORKERS = 4

# Palabras clave que delatan un anuncio de retiro / deprecacion.
_RETIRE_KEYWORDS = (
    "retire", "retirement", "retiring", "deprecat", "end of life", "end-of-life",
    "eol", "no longer supported", "unsupported", "will be removed",
    "end of support", "end-of-support", "migrate", "upgrade to", "sunset",
)

COLUMNS = [
    "service", "resource", "sku", "retireDate", "retireDateRaw",
    "status", "statusRank", "action", "problem", "category", "impact",
    "resourceGroup", "subscriptionId", "subscriptionName", "resourceId",
]


def _empty(note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    if note:
        df.attrs["note"] = note
    return df


# ---------------------------------------------------------------------------
# Extraccion de fecha de retiro
# ---------------------------------------------------------------------------
_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "iso"),
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), "dmy"),
    (re.compile(r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
                re.IGNORECASE), "month"),
]

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _parse_retire_date(ext: dict, text: str):
    """Intenta encontrar la fecha de retiro en extendedProperties o en el texto.

    Devuelve (fecha_iso|'', date|None). Nunca lanza.
    """
    # 1) Campos conocidos en extendedProperties.
    if isinstance(ext, dict):
        for k in ("retirementDate", "retireDate", "deprecationDate",
                  "endOfLifeDate", "dueDate", "date"):
            v = ext.get(k)
            if v:
                d = _try_parse_any(str(v))
                if d:
                    return d.isoformat(), d
    # 2) Buscar una fecha dentro del texto libre.
    if text:
        d = _try_parse_any(text)
        if d:
            return d.isoformat(), d
    return "", None


def _try_parse_any(s: str):
    s = s.strip()
    # ISO directo.
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:  # noqa: BLE001
        pass
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        try:
            g = m.groups()
            if kind == "iso":
                return dt.date(int(g[0]), int(g[1]), int(g[2]))
            if kind == "dmy":
                # dd/mm/yyyy (formato comun fuera de EE.UU.).
                return dt.date(int(g[2]), int(g[1]), int(g[0]))
            if kind == "month":
                month = _MONTHS.get(g[0].lower())
                if month:
                    return dt.date(int(g[2]), month, int(g[1]))
        except Exception:  # noqa: BLE001
            continue
    return None


def _status_from_date(d) -> tuple:
    """Estado segun la fecha de retiro. Devuelve (label, rank para ordenar)."""
    if d is None:
        return "En retiro", 1
    today = dt.date.today()
    if d <= today:
        return "Retirado", 2
    # Proximo (<= 180 dias) = En retiro; mas lejano = Activo (anunciado).
    if (d - today).days <= 180:
        return "En retiro", 1
    return "Activo", 0


# ---------------------------------------------------------------------------
# GET con reintento ante errores 5xx (Advisor devuelve 500 transitorios)
# ---------------------------------------------------------------------------
def _get_with_retry(credential, sub_id, url, attempts: int = 3):
    import time
    last = None
    for i in range(attempts):
        try:
            # fresh=True: cliente nuevo por llamada (evita el 500 de Advisor bajo
            # concurrencia que aparecia al compartir el cliente ARM cacheado).
            return _az._arm_get(credential, sub_id, url, fresh=True)
        except HttpResponseError as exc:
            code = getattr(exc, "status_code", None)
            last = exc
            if code and code >= 500 and i < attempts - 1:
                time.sleep(1.0)
                continue
            raise
    if last:
        raise last
    return {}


# ---------------------------------------------------------------------------
# Consulta por suscripcion
# ---------------------------------------------------------------------------
def list_deprecations_sub(credential, sub_id: str, sub_name: str = "") -> pd.DataFrame:
    """Recomendaciones de retiro/deprecacion de UNA suscripcion (Advisor).

    Nunca lanza: ante error de permisos devuelve vacio con nota.
    """
    if not _SDK_OK:
        return _empty("SDK de Azure no disponible.")

    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Advisor/recommendations"
           f"?api-version={_ADVISOR_API}")
    rows: List[dict] = []
    pages = 0
    try:
        while url and pages < 50:
            pages += 1
            payload = _get_with_retry(credential, sub_id, url)
            for rec in payload.get("value", []):
                props = rec.get("properties", {}) or {}
                short = props.get("shortDescription", {}) or {}
                problem = short.get("problem", "") or ""
                solution = short.get("solution", "") or ""
                ext = props.get("extendedProperties", {}) or {}
                blob = f"{problem} {solution} {ext}".lower()
                if not any(k in blob for k in _RETIRE_KEYWORDS):
                    continue

                res_id = (props.get("resourceMetadata", {}) or {}).get("resourceId", "") \
                    or props.get("impactedValue", "") or ""
                impacted = props.get("impactedValue", "") or \
                    _az._resource_name_from_id(res_id)
                target_type = props.get("impactedField", "") or ""
                retire_iso, retire_d = _parse_retire_date(ext, f"{problem} {solution}")
                status, srank = _status_from_date(retire_d)

                rows.append({
                    "service": _az._resource_type_pretty(target_type)
                               or props.get("category", "") or "Servicio",
                    "resource": impacted or _az._resource_name_from_id(res_id),
                    "sku": ext.get("sku") or ext.get("currentSku")
                           or ext.get("targetSku") or "",
                    "retireDate": retire_iso,
                    "retireDateRaw": retire_iso or "—",
                    "status": status,
                    "statusRank": srank,
                    "action": solution or "Revisar anuncio de Microsoft",
                    "problem": problem,
                    "category": props.get("category", "") or "",
                    "impact": props.get("impact", "") or "",
                    "resourceGroup": _az._rg_from_id(res_id),
                    "subscriptionId": sub_id,
                    "subscriptionName": sub_name or sub_id,
                    "resourceId": res_id,
                })
            url = payload.get("nextLink") or ""
        return pd.DataFrame(rows, columns=COLUMNS)
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return _empty("Sin permisos para leer Azure Advisor.")
        if code == 404:
            return _empty("Azure Advisor no disponible en la suscripcion.")
        if code and code >= 500:
            return _empty("Azure Advisor no respondió (error temporal del "
                          "servicio). Reintenta más tarde.")
        # Mensaje limpio: solo el codigo, nunca el cuerpo HTML del error.
        return _empty(f"Advisor devolvió un error (HTTP {code or '?'}).")
    except Exception as exc:  # noqa: BLE001
        # Truncar para no volcar HTML/paginas de error largas.
        msg = str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
        return _empty(f"No se pudo consultar Advisor: {msg}")


# ---------------------------------------------------------------------------
# Multi-suscripcion en paralelo
# ---------------------------------------------------------------------------
def list_deprecations(credential, subscriptions: List[dict]) -> pd.DataFrame:
    """Agrega las deprecaciones de varias suscripciones EN PARALELO."""
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    frames: List[pd.DataFrame] = []
    errors: List[str] = []

    def _one(sub):
        sid = sub.get("subscriptionId") or sub.get("id") or ""
        name = sub.get("displayName") or sub.get("name") or sid
        return name, list_deprecations_sub(credential, sid, name)

    if subs:
        workers = min(_MAX_WORKERS, len(subs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, s) for s in subs]
            for fut in as_completed(futures):
                name, df = fut.result()
                note = df.attrs.get("note")
                if note:
                    errors.append(f"{name}: {note}")
                if not df.empty:
                    frames.append(df)

    result = (pd.concat(frames, ignore_index=True) if frames else _empty())
    # Ordenar: Retirado y En retiro primero, luego por fecha mas cercana.
    if not result.empty:
        result = result.sort_values(
            ["statusRank", "retireDate"], ascending=[False, True]
        ).reset_index(drop=True)
    result.attrs["errors"] = errors
    result.attrs["subscriptions_scanned"] = len(subs)
    result.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


# ---------------------------------------------------------------------------
# Resumen para tarjetas
# ---------------------------------------------------------------------------
def summary(df: pd.DataFrame) -> dict:
    out = {"total": 0, "activo": 0, "en_retiro": 0, "retirado": 0}
    if df is None or df.empty:
        return out
    out["total"] = len(df)
    counts = df["status"].value_counts().to_dict()
    out["activo"] = int(counts.get("Activo", 0))
    out["en_retiro"] = int(counts.get("En retiro", 0))
    out["retirado"] = int(counts.get("Retirado", 0))
    return out
