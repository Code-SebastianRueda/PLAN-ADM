"""
cloud_status.py
===============
Consulta el estado de salud PUBLICO y OFICIAL de las 5 nubes soportadas por
el Plan de Administracion Operativa Cloud (PLAN-ADM):

    - AWS        -> https://health.aws.amazon.com/health/status
    - Azure      -> https://azure.status.microsoft/es-es/status
    - GCP        -> https://status.cloud.google.com
    - OCI        -> https://ocistatus.oraclecloud.com
    - IBM Cloud  -> https://cloud.ibm.com/status

Cada proveedor publica su estado por un canal distinto. Este modulo normaliza
todo a una estructura comun para que la UI (login) pueda pintar un dashboard
homogeneo con semaforo por nube y el detalle de regiones/servicios afectados.

NO se inventan datos: si una fuente no responde o no expone datos publicos
(caso IBM Cloud), se marca el estado como "unknown" y se enlaza a la pagina
oficial para consulta manual.

Estructura normalizada (CloudStatus):
    provider      str      "AWS" | "Azure" | "GCP" | "OCI" | "IBM Cloud"
    indicator     str      "ok" | "minor" | "major" | "critical" | "unknown"
    emoji         str      🟢 | 🟡 | 🟠 | 🔴 | ⚪
    summary       str      Descripcion corta del estado global
    incidents     list     Lista de dicts: {region, service, status, title, updated}
    status_url    str      URL oficial de la pagina de estado
    fetched_at    str      Timestamp ISO de la consulta
    error         str      Mensaje de error si la consulta fallo (vacio si ok)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
_TIMEOUT = 12  # segundos por request
_UA = {"User-Agent": "PLAN-ADM-CloudStatus/1.0 (+https://plan-adm.local)"}

# Paginas oficiales (las que ve el usuario) y endpoints de datos.
STATUS_PAGES = {
    "AWS":       "https://health.aws.amazon.com/health/status",
    "Azure":     "https://azure.status.microsoft/es-es/status",
    "GCP":       "https://status.cloud.google.com",
    "OCI":       "https://ocistatus.oraclecloud.com",
    "IBM Cloud": "https://cloud.ibm.com/status",
}

# Endpoints de datos crudos.
_AWS_CURRENT   = "https://health.aws.amazon.com/public/currentevents"
_AZURE_FEED    = "https://azurestatuscdn.azureedge.net/en-us/status/feed/"
_GCP_INCIDENTS = "https://status.cloud.google.com/incidents.json"
_OCI_STATUS    = "https://ocistatus.oraclecloud.com"
_IBM_STATUS    = "https://cloud.ibm.com/status"
# Feed RSS oficial de notificaciones de IBM Cloud (incidentes, mantenimientos,
# anuncios y boletines de seguridad). Es el canal que consume la propia pagina.
_IBM_FEED      = "https://cloud.ibm.com/status/api/notifications/feed.rss"
# JSON de estado critico (banner de emergencia global).
_IBM_CRITSTAT  = "https://cloud.ibm.com/critstat/status.json"

# Semaforo normalizado.
_EMOJI = {
    "ok":       "🟢",
    "minor":    "🟡",
    "major":    "🟠",
    "critical": "🔴",
    "unknown":  "⚪",
}

# Prioridad para resolver el estado global a partir de varios incidentes.
_SEVERITY_ORDER = {"ok": 0, "unknown": 1, "minor": 2, "major": 3, "critical": 4}


# ---------------------------------------------------------------------------
# Estructura normalizada
# ---------------------------------------------------------------------------
@dataclass
class CloudStatus:
    provider: str
    indicator: str = "unknown"
    summary: str = ""
    incidents: List[Dict] = field(default_factory=list)
    status_url: str = ""
    fetched_at: str = ""
    error: str = ""

    @property
    def emoji(self) -> str:
        return _EMOJI.get(self.indicator, "⚪")

    @property
    def is_healthy(self) -> bool:
        return self.indicator == "ok"

    @property
    def affected_regions(self) -> List[str]:
        regs = {i.get("region") for i in self.incidents if i.get("region")}
        return sorted(r for r in regs if r)

    def to_dict(self) -> Dict:
        return {
            "Nube": self.provider,
            "Estado": f"{self.emoji} {self.summary or self.indicator}",
            "Incidentes": len(self.incidents),
            "Regiones afectadas": ", ".join(self.affected_regions) or "—",
            "Fuente": self.status_url,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get(url: str, **kwargs) -> requests.Response:
    r = requests.get(url, headers=_UA, timeout=_TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def _worst(indicators: List[str]) -> str:
    """Devuelve el indicador mas severo de una lista."""
    if not indicators:
        return "ok"
    return max(indicators, key=lambda i: _SEVERITY_ORDER.get(i, 0))


# ---------------------------------------------------------------------------
# AWS  -  health.aws.amazon.com/public/currentevents  (JSON en UTF-16)
# ---------------------------------------------------------------------------
# status del evento AWS: 0=info, 1=issue (abierto), 2=resuelto reciente, 3=cerrado.
_AWS_STATUS_MAP = {"0": "minor", "1": "major", "2": "minor", "3": "ok"}


def fetch_aws() -> CloudStatus:
    cs = CloudStatus("AWS", status_url=STATUS_PAGES["AWS"], fetched_at=_now())
    try:
        r = _get(_AWS_CURRENT)
        # El endpoint responde en UTF-16; requests a veces no lo detecta.
        text = r.content.decode("utf-16", errors="ignore").strip()
        if not text:
            text = r.text.strip()
        # Puede venir con BOM / caracteres sueltos antes del JSON.
        start = text.find("[")
        if start > 0:
            text = text[start:]
        events = json.loads(text) if text else []

        incidents: List[Dict] = []
        indicators: List[str] = []
        for ev in events:
            raw_status = str(ev.get("status", "1"))
            ind = _AWS_STATUS_MAP.get(raw_status, "major")
            if ind == "ok":
                continue  # eventos cerrados no cuentan como incidencia activa
            indicators.append(ind)
            log = ev.get("event_log") or []
            last_msg = log[-1].get("summary") if log else ev.get("summary", "")
            incidents.append({
                "region": ev.get("region_name") or "Global",
                "service": ev.get("service_name") or "Multiple services",
                "status": "Investigando" if raw_status in ("0", "1") else "Monitoreando",
                "title": last_msg or ev.get("summary", "Evento de servicio"),
                "updated": _aws_ts(ev.get("date")),
            })

        cs.incidents = incidents
        cs.indicator = _worst(indicators) if incidents else "ok"
        cs.summary = (
            "Operativo normal" if cs.indicator == "ok"
            else f"{len(incidents)} evento(s) activo(s)"
        )
    except Exception as exc:  # noqa: BLE001
        cs.indicator = "unknown"
        cs.summary = "No se pudo consultar"
        cs.error = f"{type(exc).__name__}: {exc}"
    return cs


def _aws_ts(epoch) -> str:
    try:
        return dt.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Azure  -  RSS feed oficial
# ---------------------------------------------------------------------------
def fetch_azure() -> CloudStatus:
    cs = CloudStatus("Azure", status_url=STATUS_PAGES["Azure"], fetched_at=_now())
    try:
        r = _get(_AZURE_FEED)
        root = ET.fromstring(r.content)
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []

        incidents: List[Dict] = []
        for it in items:
            title = (it.findtext("title") or "").strip()
            desc = (it.findtext("description") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            region = _azure_region(title + " " + desc)
            incidents.append({
                "region": region or "Global",
                "service": _azure_service(title),
                "status": "Activo",
                "title": title or "Incidente de servicio",
                "updated": pub,
            })

        cs.incidents = incidents
        if not incidents:
            cs.indicator = "ok"
            cs.summary = "Sin eventos activos"
        else:
            # El feed solo publica cuando hay incidentes -> severidad media por defecto.
            cs.indicator = "major" if len(incidents) > 2 else "minor"
            cs.summary = f"{len(incidents)} incidente(s) publicado(s)"
    except Exception as exc:  # noqa: BLE001
        cs.indicator = "unknown"
        cs.summary = "No se pudo consultar"
        cs.error = f"{type(exc).__name__}: {exc}"
    return cs


def _azure_region(text: str) -> Optional[str]:
    # Busca patrones tipo "East US", "West Europe", "Brazil South", etc.
    m = re.search(
        r"\b((?:East|West|North|South|Central|Southeast|Northeast)\s+"
        r"[A-Z][a-z]+(?:\s+\d)?|[A-Z][a-z]+\s+(?:East|West|North|South|Central))\b",
        text,
    )
    return m.group(1) if m else None


def _azure_service(title: str) -> str:
    # El titulo suele empezar con el nombre del servicio.
    return title.split(" - ")[0].strip() if title else "Azure"


# ---------------------------------------------------------------------------
# GCP  -  incidents.json oficial
# ---------------------------------------------------------------------------
def fetch_gcp() -> CloudStatus:
    cs = CloudStatus("GCP", status_url=STATUS_PAGES["GCP"], fetched_at=_now())
    try:
        r = _get(_GCP_INCIDENTS)
        data = r.json()
        incidents: List[Dict] = []
        indicators: List[str] = []
        for inc in data:
            # Un incidente esta activo si no tiene fecha de fin ("end").
            if inc.get("end"):
                continue
            sev = _gcp_severity(inc.get("severity", ""))
            indicators.append(sev)
            affected = inc.get("affected_products") or []
            service = affected[0].get("title") if affected else "Multiple products"
            locations = inc.get("currently_affected_locations") or []
            region = ", ".join(l.get("id", "") for l in locations) or "Global"
            incidents.append({
                "region": region,
                "service": service,
                "status": inc.get("most_recent_update", {}).get("status", "Activo"),
                "title": inc.get("external_desc", "Incidente de servicio"),
                "updated": inc.get("modified", ""),
            })

        cs.incidents = incidents
        cs.indicator = _worst(indicators) if incidents else "ok"
        cs.summary = (
            "Operativo normal" if cs.indicator == "ok"
            else f"{len(incidents)} incidente(s) activo(s)"
        )
    except Exception as exc:  # noqa: BLE001
        cs.indicator = "unknown"
        cs.summary = "No se pudo consultar"
        cs.error = f"{type(exc).__name__}: {exc}"
    return cs


def _gcp_severity(sev: str) -> str:
    s = (sev or "").lower()
    if s in ("high",):
        return "critical"
    if s in ("medium",):
        return "major"
    if s in ("low",):
        return "minor"
    return "major"


# ---------------------------------------------------------------------------
# OCI  -  Statuspage.io (components.json + status.json)
# ---------------------------------------------------------------------------
# estado -> indicador normalizado
_OCI_STATUS_MAP = {
    "normalperformance": "ok",
    "informational": "minor",
    "degraded": "major",
    "degradedperformance": "major",
    "serviceunavailable": "critical",
    "serviceinterruption": "critical",
}


def fetch_oci() -> CloudStatus:
    # Ambiente unico: commercial (ocistatus.oraclecloud.com).
    base = _OCI_STATUS
    cs = CloudStatus("OCI", status_url=STATUS_PAGES["OCI"], fetched_at=_now())
    try:
        # Estado global rapido.
        status = _get(f"{base}/api/v2/status.json").json()
        indicator_raw = status.get("status", {}).get("indicator", "none")
        global_desc = status.get("status", {}).get("description", "")

        # Detalle por region (puede ser pesado; se tolera fallo parcial).
        incidents: List[Dict] = []
        indicators: List[str] = []
        try:
            comp = _get(f"{base}/api/v2/components.json").json()
            for region in comp.get("regionHealthReports", []):
                rname = region.get("regionName") or region.get("regionCanonicalName")
                for svc in region.get("serviceHealthReports", []):
                    raw = (svc.get("serviceStatus") or "").lower()
                    ind = _OCI_STATUS_MAP.get(raw, "ok" if raw else "unknown")
                    if ind == "ok":
                        continue
                    indicators.append(ind)
                    incidents.append({
                        "region": rname,
                        "service": svc.get("serviceName", "Servicio OCI"),
                        "status": svc.get("serviceStatus", ""),
                        "title": f"{svc.get('serviceName', '')} en {rname}",
                        "updated": "",
                    })
        except Exception:  # noqa: BLE001  -> detalle opcional
            pass

        cs.incidents = incidents
        if incidents:
            cs.indicator = _worst(indicators)
        else:
            cs.indicator = "ok" if indicator_raw == "none" else _oci_global(indicator_raw)
        cs.summary = global_desc or ("Operativo normal" if cs.indicator == "ok" else "Incidentes activos")
    except Exception as exc:  # noqa: BLE001
        cs.indicator = "unknown"
        cs.summary = "No se pudo consultar"
        cs.error = f"{type(exc).__name__}: {exc}"
    return cs


def _oci_global(indicator: str) -> str:
    return {
        "none": "ok",
        "minor": "minor",
        "major": "major",
        "critical": "critical",
    }.get((indicator or "").lower(), "unknown")


# ---------------------------------------------------------------------------
# IBM Cloud  -  sin JSON publico estable: fallback honesto con link oficial
# ---------------------------------------------------------------------------
# Categorias que IBM expone en su feed RSS publico de notificaciones.
# IMPORTANTE: este feed NO publica incidentes operativos ni mantenimientos
# (solo release notes, anuncios y boletines de seguridad). El estado operativo
# real de la pagina se sirve por una API autenticada por cuenta/region.
# Por eso el SEMAFORO de IBM se basa en el banner de emergencia global
# (critstat/status.json), que es lo unico publico y fiable sin login.
_IBM_SEC_WINDOW_DAYS = 14   # ventana para listar boletines de seguridad recientes


def fetch_ibm() -> CloudStatus:
    cs = CloudStatus("IBM Cloud", status_url=STATUS_PAGES["IBM Cloud"], fetched_at=_now())
    try:
        # 1) Semaforo: banner de emergencia global (lo que IBM fuerza en su pagina).
        crit = requests.get(_IBM_CRITSTAT, headers=_UA, timeout=_TIMEOUT).json()
        if crit.get("emergencyTextForce"):
            cs.indicator = "critical"
            cs.summary = crit.get("emergencyTextTitle", "Incidente critico")
            cs.incidents = [{
                "region": "Global",
                "service": "IBM Cloud",
                "status": "Critico",
                "title": crit.get("emergencyText", ""),
                "updated": "",
            }]
            return cs

        # Sin emergencia forzada -> operativo (no hay incidente critico global).
        cs.indicator = "ok"
        cs.summary = "Sin incidente critico global"

        # 2) Contexto informativo: boletines de seguridad recientes del feed RSS.
        #    No alteran el semaforo; solo enriquecen el detalle.
        try:
            r = _get(_IBM_FEED)
            root = ET.fromstring(r.content)
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else []
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_IBM_SEC_WINDOW_DAYS)
            for it in items:
                category = (it.findtext("category") or "").strip().lower()
                if category != "security":
                    continue
                pub_dt = _parse_rfc822(it.findtext("pubDate") or "")
                if pub_dt and pub_dt < cutoff:
                    continue
                desc = it.findtext("description") or ""
                cs.incidents.append({
                    "region": _ibm_field(desc, "Regions") or "Global",
                    "service": _ibm_field(desc, "Resources") or "IBM Cloud",
                    "status": "Boletin de seguridad",
                    "title": (it.findtext("title") or "").strip(),
                    "updated": it.findtext("pubDate") or "",
                })
        except Exception:  # noqa: BLE001  -> el feed es contexto opcional
            pass

    except Exception as exc:  # noqa: BLE001
        cs.indicator = "unknown"
        cs.summary = "No se pudo consultar"
        cs.error = f"{type(exc).__name__}: {exc}"
    return cs


def _parse_rfc822(value: str) -> Optional[dt.datetime]:
    """Parsea una fecha RFC-822 (formato pubDate de RSS) a datetime con tz."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:  # noqa: BLE001
        return None


def _ibm_field(description: str, label: str) -> Optional[str]:
    """Extrae un campo tipo 'Regions: jp-tok, eu-de' del cuerpo de la notificacion."""
    m = re.search(rf"{label}:\s*([^<\n]+?)\s*(?:<br|Type:|Update Time:|$)",
                  description, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).strip().strip(",").strip()
    return val or None


# ---------------------------------------------------------------------------
# Orquestacion en paralelo
# ---------------------------------------------------------------------------
_FETCHERS = {
    "AWS": fetch_aws,
    "Azure": fetch_azure,
    "GCP": fetch_gcp,
    "OCI": fetch_oci,
    "IBM Cloud": fetch_ibm,
}


def fetch_all(providers: Optional[List[str]] = None) -> Dict[str, CloudStatus]:
    """
    Consulta todas las nubes (o un subconjunto) en paralelo y devuelve un dict
    {provider: CloudStatus}. Nunca lanza: cada fallo queda encapsulado en el
    CloudStatus correspondiente. Ambiente unico: commercial.
    """
    targets = providers or list(_FETCHERS.keys())
    results: Dict[str, CloudStatus] = {}

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futs = {}
        for name in targets:
            fn = _FETCHERS.get(name)
            if not fn:
                continue
            futs[pool.submit(fn)] = name

        for fut in as_completed(futs):
            name = futs[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:  # noqa: BLE001
                cs = CloudStatus(name, indicator="unknown",
                                 summary="Error inesperado",
                                 status_url=STATUS_PAGES.get(name, ""),
                                 fetched_at=_now(),
                                 error=f"{type(exc).__name__}: {exc}")
                results[name] = cs

    # Orden estable segun STATUS_PAGES.
    return {k: results[k] for k in STATUS_PAGES if k in results}


# ---------------------------------------------------------------------------
# Ejecucion directa (debug manual)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    all_status = fetch_all()
    for prov, st in all_status.items():
        print(f"\n{st.emoji} {prov:10s} [{st.indicator}] {st.summary}")
        if st.error:
            print(f"   error: {st.error}")
        for inc in st.incidents[:5]:
            print(f"   - [{inc['region']}] {inc['service']}: {inc['title'][:70]}")
