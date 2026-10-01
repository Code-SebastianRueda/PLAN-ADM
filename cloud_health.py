"""
cloud_health.py
===============
Módulo 1.1 — Cloud Health (Estado de Salud de la Plataforma).

Fuentes públicas (sin autenticación), según el manual PDM:
  - AWS:   https://health.aws.amazon.com/health/status  (RSS por región/servicio)
           https://status.aws.amazon.com/rss/<service>-<region>.rss
  - Azure: https://azure.status.microsoft/en-us/status/feed  (RSS global)

Devuelve pandas.DataFrame con columnas estandarizadas:
    provider, region, service, status, severity, title, description, updated

Semáforo de severidad:
    🟢  ok         — sin eventos activos
    🟡  warning    — degradación parcial / advisory
    🔴  disrupted  — interrupción activa
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import List, Optional

import pandas as pd
import requests

# ── Constantes ────────────────────────────────────────────────────────────────
TIMEOUT = 12          # segundos por request HTTP
AWS_RSS_BASE = "https://status.aws.amazon.com/rss"
AZURE_FEED   = "https://azure.status.microsoft/en-us/status/feed"

# RSS de AWS por regiones más relevantes (clave → descripción)
# Formato de URL: https://status.aws.amazon.com/rss/<service>-<region>.rss
# Para el estado GLOBAL de múltiples servicios se usa el feed agregado:
AWS_GLOBAL_FEED = f"{AWS_RSS_BASE}/all.rss"

# Mapeo de regiones conocidas para mostrar nombre legible
AWS_REGIONS = {
    "us-east-1":      "US East (N. Virginia)",
    "us-east-2":      "US East (Ohio)",
    "us-west-1":      "US West (N. California)",
    "us-west-2":      "US West (Oregon)",
    "ca-central-1":   "Canada (Central)",
    "ca-west-1":      "Canada West (Calgary)",
    "eu-west-1":      "Europe (Ireland)",
    "eu-west-2":      "Europe (London)",
    "eu-west-3":      "Europe (Paris)",
    "eu-central-1":   "Europe (Frankfurt)",
    "eu-central-2":   "Europe (Zurich)",
    "eu-north-1":     "Europe (Stockholm)",
    "eu-south-1":     "Europe (Milan)",
    "eu-south-2":     "Europe (Spain)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-south-1":     "Asia Pacific (Mumbai)",
    "ap-south-2":     "Asia Pacific (Hyderabad)",
    "ap-east-1":      "Asia Pacific (Hong Kong)",
    "me-south-1":     "Middle East (Bahrain)",
    "me-central-1":   "Middle East (UAE)",
    "sa-east-1":      "South America (São Paulo)",
    "af-south-1":     "Africa (Cape Town)",
    "il-central-1":   "Israel (Tel Aviv)",
}

SEVERITY_MAP = {
    "disrupted":  ("🔴", "DISRUPTED"),
    "impaired":   ("🔴", "IMPAIRED"),
    "degraded":   ("🟡", "DEGRADED"),
    "warning":    ("🟡", "WARNING"),
    "advisory":   ("🟡", "ADVISORY"),
    "informational": ("🔵", "INFO"),
    "ok":         ("🟢", "OK"),
    "operational":("🟢", "OPERATIONAL"),
}


def _headers() -> dict:
    return {"User-Agent": "PDM-CloudHealth/1.0 (internal monitoring dashboard)"}


def _parse_rss(xml_text: str) -> List[dict]:
    """Parsea un feed RSS de AWS y devuelve lista de items."""
    items = []
    try:
        root = ET.fromstring(xml_text)
        ns = {"aws": "http://www.w3.org/2005/Atom"}
        channel = root.find("channel")
        if channel is None:
            return items
        for item in channel.findall("item"):
            title_el    = item.find("title")
            desc_el     = item.find("description")
            pubdate_el  = item.find("pubDate")
            link_el     = item.find("link")
            items.append({
                "title":       title_el.text.strip() if title_el is not None else "",
                "description": desc_el.text.strip()  if desc_el  is not None else "",
                "pubDate":     pubdate_el.text.strip() if pubdate_el is not None else "",
                "link":        link_el.text.strip()   if link_el  is not None else "",
            })
    except ET.ParseError:
        pass
    return items


def _severity_from_title(title: str) -> tuple:
    t = title.lower()
    for key, val in SEVERITY_MAP.items():
        if key in t:
            return val
    # Si hay "[RESOLVED]" en el título, es OK
    if "resolved" in t or "informational" in t:
        return SEVERITY_MAP["ok"]
    # Tiene texto de problema pero sin clave conocida
    if any(w in t for w in ("issue", "error", "degraded", "impaired", "disrupted", "outage")):
        return SEVERITY_MAP["disrupted"]
    return SEVERITY_MAP["ok"]


# ════════════════════════════════════════════════════════════════════════════
# AWS — estado público por regiones
# ════════════════════════════════════════════════════════════════════════════
def fetch_aws_health(regions: Optional[List[str]] = None) -> pd.DataFrame:
    """
    Consulta el RSS público de AWS Health para cada región indicada.
    Si regions=None usa la lista completa AWS_REGIONS.
    Sin autenticación, sin boto3.
    """
    cols = ["provider", "region", "regionCode", "service",
            "status", "emoji", "severity", "title", "updated", "link"]
    rows: List[dict] = []
    target_regions = regions or list(AWS_REGIONS.keys())

    # Feed global que contiene todos los eventos activos/recientes
    try:
        resp = requests.get(AWS_GLOBAL_FEED, headers=_headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        items = _parse_rss(resp.text)
    except Exception as exc:  # noqa: BLE001
        # Si falla el feed global, devolvemos filas OK para todas las regiones
        return _all_ok_aws(target_regions, str(exc))

    # Indexamos items por región para asignar estado a cada una
    region_events: dict[str, List[dict]] = {r: [] for r in target_regions}

    for item in items:
        title = item.get("title", "")
        # Buscamos la región en el título o link
        for rcode in target_regions:
            if rcode.lower() in title.lower() or rcode.lower() in item.get("link", "").lower():
                region_events[rcode].append(item)

    for rcode in target_regions:
        rname = AWS_REGIONS.get(rcode, rcode)
        events = region_events[rcode]
        if not events:
            rows.append({
                "provider": "AWS", "region": rname, "regionCode": rcode,
                "service": "All services", "status": "Operational",
                "emoji": "🟢", "severity": "OK",
                "title": "No active events", "updated": _now(), "link": "",
            })
        else:
            # Tomamos el evento más reciente con mayor severidad
            for ev in events[:3]:  # máx 3 eventos por región
                emoji, sev = _severity_from_title(ev["title"])
                rows.append({
                    "provider": "AWS", "region": rname, "regionCode": rcode,
                    "service": _extract_service(ev["title"]),
                    "status": sev, "emoji": emoji, "severity": sev,
                    "title": ev["title"][:120],
                    "updated": ev.get("pubDate", "")[:30],
                    "link": ev.get("link", ""),
                })

    df = pd.DataFrame(rows, columns=cols)
    df.attrs["fetched_at"] = _now()
    df.attrs["source"] = AWS_GLOBAL_FEED
    return df


def _all_ok_aws(regions: List[str], error: str = "") -> pd.DataFrame:
    cols = ["provider", "region", "regionCode", "service",
            "status", "emoji", "severity", "title", "updated", "link"]
    rows = [{
        "provider": "AWS", "region": AWS_REGIONS.get(r, r), "regionCode": r,
        "service": "All services", "status": "Unknown",
        "emoji": "⚪", "severity": "UNKNOWN",
        "title": f"No se pudo obtener el estado: {error}",
        "updated": _now(), "link": "",
    } for r in regions]
    return pd.DataFrame(rows, columns=cols)


def _extract_service(title: str) -> str:
    """Extrae el nombre del servicio del título del RSS si es posible."""
    # Títulos típicos: "[RESOLVED] Amazon EC2 – us-east-1"
    title = title.replace("[RESOLVED]", "").strip()
    parts = title.split("–")[0].split("-")[0].strip()
    return parts[:60] if parts else title[:60]


# ════════════════════════════════════════════════════════════════════════════
# Azure — estado público global
# ════════════════════════════════════════════════════════════════════════════
AZURE_REGIONS = [
    "East US", "East US 2", "West US", "West US 2", "West US 3",
    "Central US", "North Central US", "South Central US",
    "West Central US", "Canada Central", "Canada East",
    "Brazil South", "Brazil Southeast",
    "North Europe", "West Europe", "UK South", "UK West",
    "France Central", "France South", "Germany West Central",
    "Germany North", "Switzerland North", "Switzerland West",
    "Norway East", "Norway West", "Sweden Central", "Poland Central",
    "Italy North", "Spain Central",
    "East Asia", "Southeast Asia", "Japan East", "Japan West",
    "Australia East", "Australia Southeast", "Australia Central",
    "Central India", "South India", "West India",
    "Korea Central", "Korea South",
    "South Africa North", "South Africa West",
    "UAE North", "UAE Central",
    "Israel Central", "Qatar Central",
]


def fetch_azure_health() -> pd.DataFrame:
    """
    Consulta el RSS público de Azure Status.
    Si no hay eventos activos, marca todas las regiones como OK.
    """
    cols = ["provider", "region", "service", "status",
            "emoji", "severity", "title", "updated", "link"]
    rows: List[dict] = []

    try:
        resp = requests.get(AZURE_FEED, headers=_headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        items = _parse_rss(resp.text)
    except Exception as exc:  # noqa: BLE001
        return _all_ok_azure(str(exc))

    if not items:
        # Feed vacío = no hay incidentes activos
        return _all_ok_azure()

    # Hay incidentes: los parseamos
    region_events: dict[str, List[dict]] = {r: [] for r in AZURE_REGIONS}
    global_events = []

    for item in items:
        title = item.get("title", "")
        desc  = item.get("description", "")
        matched = False
        for region in AZURE_REGIONS:
            if region.lower() in title.lower() or region.lower() in desc.lower():
                region_events[region].append(item)
                matched = True
        if not matched:
            global_events.append(item)

    # Regiones con eventos
    affected_regions = set()
    for region, events in region_events.items():
        for ev in events[:2]:
            emoji, sev = _severity_from_title(ev["title"])
            rows.append({
                "provider": "Azure", "region": region,
                "service": _extract_service(ev["title"]),
                "status": sev, "emoji": emoji, "severity": sev,
                "title": ev["title"][:120],
                "updated": ev.get("pubDate", "")[:30],
                "link": ev.get("link", ""),
            })
            affected_regions.add(region)

    # Regiones sin eventos → OK
    for region in AZURE_REGIONS:
        if region not in affected_regions:
            rows.append({
                "provider": "Azure", "region": region,
                "service": "All services", "status": "Operational",
                "emoji": "🟢", "severity": "OK",
                "title": "No active events",
                "updated": _now(), "link": "",
            })

    # Eventos globales (sin región específica)
    for ev in global_events[:5]:
        emoji, sev = _severity_from_title(ev["title"])
        rows.append({
            "provider": "Azure", "region": "Global",
            "service": _extract_service(ev["title"]),
            "status": sev, "emoji": emoji, "severity": sev,
            "title": ev["title"][:120],
            "updated": ev.get("pubDate", "")[:30],
            "link": ev.get("link", ""),
        })

    df = pd.DataFrame(rows, columns=cols)
    df.attrs["fetched_at"] = _now()
    df.attrs["source"] = AZURE_FEED
    return df


def _all_ok_azure(error: str = "") -> pd.DataFrame:
    cols = ["provider", "region", "service", "status",
            "emoji", "severity", "title", "updated", "link"]
    if error:
        rows = [{
            "provider": "Azure", "region": "Global",
            "service": "Status feed", "status": "UNKNOWN",
            "emoji": "⚪", "severity": "UNKNOWN",
            "title": f"No se pudo obtener el estado: {error}",
            "updated": _now(), "link": AZURE_FEED,
        }]
        return pd.DataFrame(rows, columns=cols)

    rows = [{
        "provider": "Azure", "region": r,
        "service": "All services", "status": "Operational",
        "emoji": "🟢", "severity": "OK",
        "title": "No active events", "updated": _now(), "link": "",
    } for r in AZURE_REGIONS]
    return pd.DataFrame(rows, columns=cols)


# ════════════════════════════════════════════════════════════════════════════
# Vista consolidada (AWS + Azure)
# ════════════════════════════════════════════════════════════════════════════
def fetch_all_health(aws_regions: Optional[List[str]] = None) -> pd.DataFrame:
    """Combina AWS + Azure en un solo DataFrame."""
    aws_df   = fetch_aws_health(aws_regions)
    azure_df = fetch_azure_health()
    # Alineamos columnas
    common = ["provider", "region", "service", "status",
              "emoji", "severity", "title", "updated"]
    dfs = []
    for df in [aws_df, azure_df]:
        available = [c for c in common if c in df.columns]
        dfs.append(df[available])
    result = pd.concat(dfs, ignore_index=True)
    result.attrs["fetched_at"] = _now()
    return result


def summary_by_provider(df: pd.DataFrame) -> pd.DataFrame:
    """Resumen ejecutivo: total OK / Warning / Disrupted por proveedor."""
    if df.empty or "severity" not in df.columns:
        return pd.DataFrame()
    grp = df.groupby(["provider", "severity"]).size().reset_index(name="count")
    return grp.sort_values(["provider", "count"], ascending=[True, False]).reset_index(drop=True)


def active_incidents(df: pd.DataFrame) -> pd.DataFrame:
    """Filtra sólo los registros con problemas (no OK / no Operational)."""
    if df.empty or "severity" not in df.columns:
        return pd.DataFrame()
    return df[~df["severity"].isin(["OK", "OPERATIONAL"])].copy()


def _now() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
