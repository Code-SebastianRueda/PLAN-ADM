"""
azure_inventory.py
==================
Inventario de recursos Azure (apartado 1.4) — DATOS REALES vía Azure Resource Graph.

En vez de recorrer tipo por tipo (VMs, discos, etc.), consulta Azure Resource
Graph: una sola query KQL devuelve TODOS los recursos del alcance con su tipo,
resource group, region, suscripcion, tags y fecha de creacion cuando el recurso
la expone (properties.timeCreated). Es la via oficial y mas eficiente para un
inventario completo.

Endpoint (global, no por-suscripcion):
    POST /providers/Microsoft.ResourceGraph/resources?api-version=2024-04-01
    body: {"subscriptions": [...], "query": "<KQL>", "options": {...}}

Multi-suscripcion: Resource Graph acepta hasta ~1000 subs por request; se envian
todas en un solo POST (con paginacion por $skipToken). Nunca inventa datos: si un
recurso no expone timeCreated, su fecha queda vacia.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import pandas as pd

import azure_alerts as _az  # reutiliza _arm_post, _get_client, helpers

try:
    from azure.core.exceptions import HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_RG_API = "2024-04-01"
_RG_URL = (f"https://management.azure.com/providers/Microsoft.ResourceGraph/"
           f"resources?api-version={_RG_API}")

# KQL: proyecta los campos del inventario. timeCreated solo existe para algunos
# tipos; se extrae de properties con coalesce de los nombres habituales.
_KQL = (
    "Resources "
    "| extend created = coalesce("
    "   todatetime(properties.timeCreated), "
    "   todatetime(properties.createdTime), "
    "   todatetime(properties.creationTime)) "
    "| project name, type, resourceGroup, location, subscriptionId, "
    "   tags, created, kind, sku, id "
    "| order by name asc"
)

COLUMNS = [
    "name", "type", "typePretty", "category", "resourceGroup", "location",
    "locationPretty", "subscriptionId", "subscriptionName", "created",
    "createdDate", "tagCount", "hasOwner", "id",
]

# Prettify de regiones Azure comunes.
_REGION_PRETTY = {
    "eastus": "East US", "eastus2": "East US 2", "westus": "West US",
    "westus2": "West US 2", "westus3": "West US 3", "centralus": "Central US",
    "southcentralus": "South Central US", "northcentralus": "North Central US",
    "westeurope": "West Europe", "northeurope": "North Europe",
    "brazilsouth": "Brazil South", "uksouth": "UK South", "ukwest": "UK West",
    "canadacentral": "Canada Central", "australiaeast": "Australia East",
    "eastasia": "East Asia", "southeastasia": "Southeast Asia",
    "japaneast": "Japan East", "centralindia": "Central India",
    "francecentral": "France Central", "germanywestcentral": "Germany West Central",
    "global": "Global",
}

# Categoria de alto nivel por prefijo de tipo (para filtros tipo mockup).
_CATEGORY_MAP = {
    "microsoft.compute": "Compute",
    "microsoft.containerservice": "Compute",
    "microsoft.web": "Compute",
    "microsoft.network": "Networking",
    "microsoft.storage": "Storage",
    "microsoft.sql": "Database",
    "microsoft.documentdb": "Database",
    "microsoft.dbforpostgresql": "Database",
    "microsoft.dbformysql": "Database",
    "microsoft.cache": "Database",
    "microsoft.keyvault": "Security",
    "microsoft.security": "Security",
    "microsoft.insights": "Monitoring",
    "microsoft.operationalinsights": "Monitoring",
}


def _pretty_region(loc: str) -> str:
    if not loc:
        return "—"
    return _REGION_PRETTY.get(loc.lower(), loc)


def _pretty_type(rtype: str) -> str:
    """'microsoft.compute/virtualmachines' -> 'Virtual Machine' aprox."""
    return _az._resource_type_pretty(rtype) if rtype else ""


def _category_of(rtype: str) -> str:
    if not rtype:
        return "Otros"
    provider = rtype.split("/")[0].lower()
    return _CATEGORY_MAP.get(provider, "Otros")


def _empty(note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    if note:
        df.attrs["note"] = note
    return df


def list_resources(credential, subscriptions: List[dict]) -> pd.DataFrame:
    """Inventario COMPLETO de recursos del alcance via Resource Graph.

    subscriptions: lista de {'subscriptionId','displayName'}. Devuelve un
    DataFrame con una fila por recurso. Nunca lanza: errores en attrs['errors'].
    """
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    if not subs or not _SDK_OK:
        df = _empty("Sin suscripciones o SDK no disponible.")
        df.attrs["errors"] = []
        df.attrs["subscriptions_scanned"] = 0
        df.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return df

    sub_ids = [s.get("subscriptionId") or s.get("id") for s in subs]
    name_by_id = {(s.get("subscriptionId") or s.get("id")):
                  (s.get("displayName") or s.get("name") or
                   (s.get("subscriptionId") or s.get("id"))) for s in subs}

    rows: List[dict] = []
    errors: List[str] = []
    skip_token = None
    pages = 0
    # Un sub_id cualquiera para construir el pipeline autenticado del POST.
    pipeline_sub = sub_ids[0]

    try:
        while pages < 100:
            pages += 1
            body = {
                "subscriptions": sub_ids,
                "query": _KQL,
                "options": {"resultFormat": "objectArray", "$top": 1000},
            }
            if skip_token:
                body["options"]["$skipToken"] = skip_token
            payload = _az._arm_post(credential, pipeline_sub, _RG_URL, body)
            data = payload.get("data", [])
            # resultFormat objectArray -> data es lista de dicts.
            if isinstance(data, dict):
                # formato 'table': columns + rows
                cols = [c["name"] for c in data.get("columns", [])]
                recs = [dict(zip(cols, r)) for r in data.get("rows", [])]
            else:
                recs = data
            for rec in recs:
                rtype = rec.get("type", "") or ""
                loc = rec.get("location", "") or ""
                sid = rec.get("subscriptionId", "") or ""
                tags = rec.get("tags") or {}
                created = rec.get("created") or ""
                created_date = str(created)[:10] if created else ""
                tag_keys = {k.lower() for k in (tags.keys() if isinstance(tags, dict) else [])}
                rows.append({
                    "name": rec.get("name", "") or "",
                    "type": rtype,
                    "typePretty": _pretty_type(rtype),
                    "category": _category_of(rtype),
                    "resourceGroup": rec.get("resourceGroup", "") or "",
                    "location": loc,
                    "locationPretty": _pretty_region(loc),
                    "subscriptionId": sid,
                    "subscriptionName": name_by_id.get(sid, sid),
                    "created": created or "",
                    "createdDate": created_date,
                    "tagCount": len(tags) if isinstance(tags, dict) else 0,
                    "hasOwner": ("owner" in tag_keys or "propietario" in tag_keys),
                    "id": rec.get("id", "") or "",
                })
            skip_token = payload.get("$skipToken") or payload.get("skipToken")
            if not skip_token:
                break
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            errors.append("Sin permisos para Resource Graph.")
        elif code == 404:
            errors.append("Resource Graph no disponible.")
        else:
            errors.append(f"Error en Resource Graph: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df.attrs["errors"] = errors
    df.attrs["subscriptions_scanned"] = len(subs)
    df.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return df


# ---------------------------------------------------------------------------
# Derivados para el dashboard
# ---------------------------------------------------------------------------
def kpis(df: pd.DataFrame) -> dict:
    out = {"total": 0, "nuevos": 0, "subs": 0, "resourceGroups": 0,
           "regiones": 0, "tipos": 0, "categorias": 0}
    if df is None or df.empty:
        return out
    out["total"] = len(df)
    out["subs"] = int(df["subscriptionId"].nunique())
    out["resourceGroups"] = int(df["resourceGroup"].replace("", pd.NA).nunique())
    out["regiones"] = int(df["location"].replace("", pd.NA).nunique())
    out["tipos"] = int(df["type"].nunique())
    out["categorias"] = int(df["category"].nunique())
    out["nuevos"] = int(_recent_mask(df, 7).sum())
    return out


def _recent_mask(df: pd.DataFrame, days: int):
    """Mascara de recursos creados en los ultimos `days` (si tienen fecha)."""
    if df is None or df.empty or "created" not in df.columns:
        return pd.Series([], dtype=bool)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    def _is_recent(v):
        d = _az._parse_iso(str(v)) if v else None
        if d is None:
            return False
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d >= cutoff
    return df["created"].map(_is_recent)


def by_type(df: pd.DataFrame, top: int = 8) -> pd.DataFrame:
    cols = ["typePretty", "count"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.groupby("typePretty").size().reset_index(name="count")
           .sort_values("count", ascending=False).head(top).reset_index(drop=True))
    return g[cols]


def by_subscription(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["subscriptionName", "count", "pct", "nuevos"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    recent = _recent_mask(df, 7)
    g = (df.assign(_new=recent.astype(int))
           .groupby("subscriptionName")
           .agg(count=("id", "count"), nuevos=("_new", "sum"))
           .reset_index())
    total = g["count"].sum()
    g["pct"] = (g["count"] / total * 100).round(0).astype(int) if total else 0
    g = g.sort_values("count", ascending=False).reset_index(drop=True)
    return g[cols]


def by_region(df: pd.DataFrame, top: int = 6) -> pd.DataFrame:
    cols = ["locationPretty", "count"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.groupby("locationPretty").size().reset_index(name="count")
           .sort_values("count", ascending=False).reset_index(drop=True))
    if len(g) > top:
        head = g.head(top - 1).copy()
        other = pd.DataFrame([{"locationPretty": "Otras",
                               "count": int(g["count"][top - 1:].sum())}])
        g = pd.concat([head, other], ignore_index=True)
    return g[cols]


def recent_resources(df: pd.DataFrame, days: int = 7, top: int = 50) -> pd.DataFrame:
    cols = ["name", "typePretty", "type", "subscriptionName", "resourceGroup",
            "locationPretty", "created", "createdAgo"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    mask = _recent_mask(df, days)
    g = df[mask].copy()
    if g.empty:
        return pd.DataFrame(columns=cols)
    g["createdAgo"] = g["created"].map(
        lambda v: _az._humanize_delta(_az._parse_iso(str(v))) if v else "")
    g = g.sort_values("created", ascending=False).head(top).reset_index(drop=True)
    return g[cols]


def categories(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty:
        return []
    return sorted({c for c in df["category"].dropna().tolist() if c})
