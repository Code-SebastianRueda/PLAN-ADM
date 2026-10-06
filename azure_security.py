"""
azure_security.py
=================
Azure Security Center (apartado 1.3) — DATOS REALES.

Combina dos fuentes de Azure en una sola vista de postura de seguridad:

  Microsoft Defender for Cloud
    - Microsoft.Security/assessments  -> recomendaciones por recurso
      (displayName, severity, status Healthy/Unhealthy, recurso afectado).
    - Microsoft.Security/secureScores/ascScore -> Secure Score real (0-100).

  Azure Advisor
    - Microsoft.Advisor/recommendations (category = Security) -> recomendaciones
      de seguridad (problem/solution, impact, recurso).

Ambas se normalizan a un esquema unico de "recomendaciones" para poder
mezclarlas, agrupar por severidad/origen/tipo de recurso y priorizar.

Multi-suscripcion EN PARALELO. Reutiliza _arm_get del modulo de alertas (cliente
ARM cacheado). Nunca inventa datos: si una fuente falla por permisos, se marca.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import pandas as pd

import azure_alerts as _az  # _arm_get, cliente cacheado, helpers

try:
    from azure.core.exceptions import HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_ASSESS_API = "2021-06-01"
_SCORE_API = "2020-01-01"
_ADVISOR_API = "2023-01-01"
_MAX_WORKERS = 16

# Severidad -> (etiqueta, codigo interno, rank). Defender usa High/Medium/Low.
_SEV_NORM = {
    "high": ("Crítica", "critical", 0),
    "critical": ("Crítica", "critical", 0),
    "medium": ("Alta", "high", 1),
    "low": ("Media", "medium", 2),
    "informational": ("Informativa", "info", 3),
}
# Advisor impact High/Medium/Low -> severidad de seguridad.
_ADVISOR_SEV = {
    "high": ("Alta", "high", 1),
    "medium": ("Media", "medium", 2),
    "low": ("Informativa", "info", 3),
}

SEV_LABELS = ["Crítica", "Alta", "Media", "Informativa"]
SEV_RANK = {"Crítica": 0, "Alta": 1, "Media": 2, "Informativa": 3}

COLUMNS = [
    "name", "origin", "severity", "sevRank", "status", "statusRaw",
    "resource", "resourceType", "resourceId", "resourceGroup",
    "subscriptionId", "subscriptionName", "description", "remediation",
    "impact", "lastFound", "assessmentId", "category",
]


def _empty(note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    if note:
        df.attrs["note"] = note
    return df


# ---------------------------------------------------------------------------
# Defender for Cloud: assessments
# ---------------------------------------------------------------------------
def _assessments_sub(credential, sub_id, sub_name):
    """Recomendaciones de Defender (assessments) de UNA suscripcion.

    Devuelve (rows, note|None). Solo incluye las UNHEALTHY (pendientes); las
    healthy se cuentan aparte para 'corregidas'.
    """
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Security/assessments"
           f"?api-version={_ASSESS_API}")
    rows, healthy = [], 0
    try:
        payload = _az._arm_get(credential, sub_id, url)
        for a in payload.get("value", []):
            props = a.get("properties", {}) or {}
            status = props.get("status", {}) or {}
            code = (status.get("code", "") or "")  # Healthy/Unhealthy/NotApplicable
            sev_raw = (status.get("severity", "")
                       or props.get("metadata", {}).get("severity", "") or "")
            if code.lower() == "healthy":
                healthy += 1
                continue
            if code.lower() not in ("unhealthy",):
                continue  # NotApplicable u otros -> no son hallazgos pendientes
            meta = props.get("metadata", {}) or {}
            display = (props.get("displayName", "")
                       or meta.get("displayName", "")
                       or a.get("name", ""))
            res_id = (props.get("resourceDetails", {}) or {}).get("id", "") \
                or _resource_from_assessment_id(a.get("id", ""))
            sev_label, _code, rank = _SEV_NORM.get(
                sev_raw.lower(), ("Media", "medium", 2))
            rows.append({
                "name": display,
                "origin": "Defender",
                "severity": sev_label,
                "sevRank": rank,
                "status": "Pendiente",
                "statusRaw": code,
                "resource": _az._resource_name_from_id(res_id),
                "resourceType": _az._resource_type_pretty(
                    _type_from_id(res_id)),
                "resourceId": res_id,
                "resourceGroup": _az._rg_from_id(res_id),
                "subscriptionId": sub_id,
                "subscriptionName": sub_name,
                "description": meta.get("description", "") or "",
                "remediation": meta.get("remediationDescription", "") or "",
                "impact": meta.get("categories", [""])[0]
                          if meta.get("categories") else "",
                "lastFound": a.get("properties", {}).get("timeGenerated", "") or "",
                "assessmentId": a.get("id", "") or "",
                "category": "Security",
            })
        df = pd.DataFrame(rows, columns=COLUMNS)
        df.attrs["healthy"] = healthy
        return df, None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return _empty(), "Sin permisos para Defender for Cloud."
        if code == 404:
            return _empty(), "Defender for Cloud no disponible."
        return _empty(), f"Error Defender: {exc}"
    except Exception as exc:  # noqa: BLE001
        return _empty(), f"{type(exc).__name__}: {exc}"


def _type_from_id(res_id: str) -> str:
    if not res_id or "/providers/" not in res_id:
        return ""
    tail = res_id.split("/providers/")[-1]
    parts = tail.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return tail


def _resource_from_assessment_id(aid: str) -> str:
    """El id del assessment suele ser <resourceId>/providers/Microsoft.Security/
    assessments/<guid>. Extrae el resourceId."""
    if not aid:
        return ""
    marker = "/providers/Microsoft.Security/assessments/"
    if marker in aid:
        return aid.split(marker)[0]
    return ""


def _secure_score_sub(credential, sub_id):
    """Secure Score (0-100) de la suscripcion. None si no disponible."""
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Security/secureScores/"
           f"ascScore?api-version={_SCORE_API}")
    try:
        payload = _az._arm_get(credential, sub_id, url)
        score = (payload.get("properties", {}) or {}).get("score", {}) or {}
        pct = score.get("percentage")
        if pct is None:
            cur, mx = score.get("current"), score.get("max")
            pct = (cur / mx) if (cur is not None and mx) else None
        return round(float(pct) * 100, 1) if pct is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Advisor: recomendaciones de seguridad
# ---------------------------------------------------------------------------
def _advisor_security_sub(credential, sub_id, sub_name):
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Advisor/recommendations"
           f"?api-version={_ADVISOR_API}")
    rows = []
    try:
        next_url = url
        pages = 0
        while next_url and pages < 20:
            pages += 1
            payload = _az._arm_get(credential, sub_id, next_url)
            for rec in payload.get("value", []):
                props = rec.get("properties", {}) or {}
                category = (props.get("category", "") or "")
                if category.lower() != "security":
                    continue
                short = props.get("shortDescription", {}) or {}
                impact = (props.get("impact", "") or "")
                sev_label, _c, rank = _ADVISOR_SEV.get(
                    impact.lower(), ("Media", "medium", 2))
                res_id = (props.get("resourceMetadata", {}) or {}).get("resourceId", "") \
                    or props.get("impactedValue", "") or ""
                rows.append({
                    "name": short.get("problem", "") or "Recomendación de seguridad",
                    "origin": "Advisor",
                    "severity": sev_label,
                    "sevRank": rank,
                    "status": "Pendiente",
                    "statusRaw": "Active",
                    "resource": props.get("impactedValue", "")
                                or _az._resource_name_from_id(res_id),
                    "resourceType": _az._resource_type_pretty(
                        props.get("impactedField", "")),
                    "resourceId": res_id,
                    "resourceGroup": _az._rg_from_id(res_id),
                    "subscriptionId": sub_id,
                    "subscriptionName": sub_name,
                    "description": short.get("problem", "") or "",
                    "remediation": short.get("solution", "") or "",
                    "impact": impact,
                    "lastFound": props.get("lastUpdated", "") or "",
                    "assessmentId": rec.get("id", "") or "",
                    "category": "Security",
                })
            next_url = payload.get("nextLink") or ""
        return pd.DataFrame(rows, columns=COLUMNS), None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return _empty(), "Sin permisos para Azure Advisor."
        if code == 404:
            return _empty(), "Azure Advisor no disponible."
        return _empty(), f"Error Advisor: {exc}"
    except Exception as exc:  # noqa: BLE001
        return _empty(), f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Orquestacion multi-suscripcion
# ---------------------------------------------------------------------------
def load_security(credential, subscriptions: List[dict]) -> pd.DataFrame:
    """Carga y combina Defender + Advisor de todas las suscripciones en paralelo.

    Devuelve un DataFrame de recomendaciones con attrs:
      - errors: list de notas por fuente/sub
      - secure_score: promedio ponderado disponible (o None)
      - healthy_total: assessments healthy (proxy de 'corregidas')
      - defender_status / advisor_status: 'ok' | 'error'
      - fetched_at
    """
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    frames: List[pd.DataFrame] = []
    errors: List[str] = []
    scores: List[float] = []
    healthy_total = 0
    defender_ok = False
    advisor_ok = False

    if not subs or not _SDK_OK:
        df = _empty("Sin suscripciones o SDK no disponible.")
        df.attrs.update(errors=[], secure_score=None, healthy_total=0,
                        defender_status="error", advisor_status="error",
                        subscriptions_scanned=0,
                        fetched_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return df

    def _work(sub):
        sid = sub.get("subscriptionId") or sub.get("id")
        name = sub.get("displayName") or sub.get("name") or sid
        out = {"name": name}
        d_df, d_note = _assessments_sub(credential, sid, name)
        a_df, a_note = _advisor_security_sub(credential, sid, name)
        out["defender"] = (d_df, d_note)
        out["advisor"] = (a_df, a_note)
        out["score"] = _secure_score_sub(credential, sid)
        return out

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(subs))) as pool:
        futures = [pool.submit(_work, s) for s in subs]
        for fut in as_completed(futures):
            r = fut.result()
            name = r["name"]
            d_df, d_note = r["defender"]
            a_df, a_note = r["advisor"]
            if d_note:
                errors.append(f"Defender · {name}: {d_note}")
            else:
                defender_ok = True
                healthy_total += int(d_df.attrs.get("healthy", 0))
                if not d_df.empty:
                    frames.append(d_df)
            if a_note:
                errors.append(f"Advisor · {name}: {a_note}")
            else:
                advisor_ok = True
                if not a_df.empty:
                    frames.append(a_df)
            if r["score"] is not None:
                scores.append(r["score"])

    result = pd.concat(frames, ignore_index=True) if frames else _empty()
    result.attrs["errors"] = errors
    result.attrs["secure_score"] = (round(sum(scores) / len(scores), 1)
                                    if scores else None)
    result.attrs["healthy_total"] = healthy_total
    result.attrs["defender_status"] = "ok" if defender_ok else "error"
    result.attrs["advisor_status"] = "ok" if advisor_ok else "error"
    result.attrs["subscriptions_scanned"] = len(subs)
    result.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


# ---------------------------------------------------------------------------
# Derivados para el dashboard
# ---------------------------------------------------------------------------
def kpis(df: pd.DataFrame) -> dict:
    out = {"secure_score": None, "criticas": 0, "altas": 0,
           "altas_recursos": 0, "recomendaciones": 0, "recursos": 0,
           "corregidas": 0}
    if df is None:
        return out
    out["secure_score"] = df.attrs.get("secure_score")
    out["corregidas"] = int(df.attrs.get("healthy_total", 0))
    if df.empty:
        return out
    out["recomendaciones"] = len(df)
    out["criticas"] = int((df["severity"] == "Crítica").sum())
    altas = df[df["severity"] == "Alta"]
    out["altas"] = int(len(altas))
    out["altas_recursos"] = int(altas["resourceId"].replace("", pd.NA).nunique())
    out["recursos"] = int(df["resourceId"].replace("", pd.NA).nunique())
    return out


def by_severity(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["severity", "count", "pct"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    g = df.groupby("severity").size().reset_index(name="count")
    total = g["count"].sum()
    g["pct"] = (g["count"] / total * 100).round(1) if total else 0
    g["rank"] = g["severity"].map(SEV_RANK).fillna(9)
    g = g.sort_values("rank").drop(columns="rank").reset_index(drop=True)
    return g[cols]


def by_origin(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["origin", "count", "recursos", "pct"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    total = len(df)
    g = (df.groupby("origin")
           .agg(count=("name", "count"),
                recursos=("resourceId", lambda s: s.replace("", pd.NA).nunique()))
           .reset_index())
    g["pct"] = (g["count"] / total * 100).round(0).astype(int)
    return g[cols].sort_values("count", ascending=False).reset_index(drop=True)


def top_resource_types(df: pd.DataFrame, top: int = 8) -> pd.DataFrame:
    cols = ["resourceType", "recursos", "hallazgos"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    d = df[df["resourceId"].astype(bool)]
    if d.empty:
        return pd.DataFrame(columns=cols)
    g = (d.groupby("resourceType")
           .agg(recursos=("resourceId", lambda s: s.nunique()),
                hallazgos=("name", "count"))
           .reset_index()
           .sort_values("recursos", ascending=False).head(top)
           .reset_index(drop=True))
    g["resourceType"] = g["resourceType"].replace("", "Otros")
    return g[cols]


def priority_recommendations(df: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """Recomendaciones agrupadas por nombre, priorizadas por severidad y nº de
    recursos afectados (prioridad = peso_severidad * 100 + recursos)."""
    cols = ["name", "origin", "severity", "sevRank", "recursos",
            "lastFound", "subscriptionName", "remediation", "assessmentId"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.groupby(["name", "origin", "severity", "sevRank"])
           .agg(recursos=("resourceId", lambda s: s.replace("", pd.NA).nunique()),
                lastFound=("lastFound", "max"),
                subscriptionName=("subscriptionName", "first"),
                remediation=("remediation", "first"),
                assessmentId=("assessmentId", "first"))
           .reset_index())
    weight = {0: 400, 1: 300, 2: 200, 3: 100}
    g["_prio"] = g["sevRank"].map(lambda r: weight.get(int(r), 100)) + g["recursos"]
    g = g.sort_values("_prio", ascending=False).drop(columns="_prio")
    return g.head(top).reset_index(drop=True)[cols]


def affected_resources(df: pd.DataFrame) -> pd.DataFrame:
    """Un registro por recurso afectado, con su riesgo max y nº de hallazgos."""
    cols = ["resource", "resourceType", "subscriptionName", "resourceGroup",
            "riesgo", "riesgoRank", "hallazgos", "resourceId"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    d = df[df["resourceId"].astype(bool)].copy()
    if d.empty:
        return pd.DataFrame(columns=cols)
    g = (d.groupby(["resourceId", "resource", "resourceType",
                    "subscriptionName", "resourceGroup"])
           .agg(riesgoRank=("sevRank", "min"), hallazgos=("name", "count"))
           .reset_index())
    rank_label = {0: "Crítico", 1: "Alto", 2: "Medio", 3: "Informativo"}
    g["riesgo"] = g["riesgoRank"].map(lambda r: rank_label.get(int(r), "Medio"))
    g = g.sort_values(["riesgoRank", "hallazgos"],
                      ascending=[True, False]).reset_index(drop=True)
    return g[cols]


def resource_types(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty:
        return []
    return sorted({t for t in df["resourceType"].dropna().tolist() if t})
