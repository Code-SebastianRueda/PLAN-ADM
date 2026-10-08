"""
azure_alerts.py
===============
Azure Alert Center — capa de DATOS REALES para el apartado 1.5 Alertamiento.

Consulta Azure Monitor Alerts (proveedor Microsoft.AlertsManagement) a traves
del Azure Resource Manager, usando la credencial ya autenticada en el login.
No usa datos simulados: todo proviene de la API real. Si falta permiso o la
suscripcion no responde, se devuelve vacio con una nota — nunca se inventa.

Endpoints usados (ARM):
    GET /subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts
        ?api-version=2019-05-05-preview&timeRange=...&monitorCondition=...
        -> lista de alertas con properties.essentials (severidad, estado,
           recurso objetivo, tiempos de disparo, etc.)
    GET .../alerts/getSummary?...&groupby=severity,alertState
        -> conteos agregados por severidad y estado (rapido, sin paginar).

Diseño:
    - Una ApiClient ligera por suscripcion (reusa el pipeline del
      MonitorManagementClient, que ya aporta auth + reintentos).
    - Todas las lecturas devuelven pandas.DataFrame; los resumenes, dicts.
    - Tolerante a errores por suscripcion (se agregan a .attrs['errors']).

Depende solo de azure-mgmt-monitor (ya instalado) + pandas. No requiere
azure-mgmt-alertsmanagement.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd

# Maximo de suscripciones consultadas en paralelo. Alto porque son operaciones
# de red (I/O bound), no de CPU: el GIL no las serializa mientras esperan.
_MAX_WORKERS = 16

try:
    from azure.mgmt.monitor import MonitorManagementClient
    from azure.mgmt.resource import ResourceManagementClient
    from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_API_VERSION = "2019-05-05-preview"

# Mapeo de severidad de Azure (Sev0..Sev4) a etiquetas legibles y orden.
SEV_LABEL = {
    "Sev0": "Critical",
    "Sev1": "Error",
    "Sev2": "Warning",
    "Sev3": "Informational",
    "Sev4": "Verbose",
}
SEV_ORDER = {"Sev0": 0, "Sev1": 1, "Sev2": 2, "Sev3": 3, "Sev4": 4}

# Rango de tiempo -> valor aceptado por la API (timeRange) y etiqueta UI.
TIME_RANGES = {
    "Última hora": "1h",
    "Últimas 6 horas": "1d",      # la API no tiene 6h; se filtra en cliente
    "Últimas 24 horas": "1d",
    "Últimos 7 días": "7d",
    "Últimos 30 días": "30d",
}


class AzureAlertsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rg_from_id(resource_id: Optional[str]) -> str:
    if not resource_id:
        return ""
    parts = resource_id.split("/")
    low = [p.lower() for p in parts]
    try:
        return parts[low.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return ""


def _resource_name_from_id(resource_id: Optional[str]) -> str:
    if not resource_id:
        return ""
    return resource_id.rstrip("/").split("/")[-1]


def _resource_type_pretty(target_type: Optional[str]) -> str:
    """'microsoft.compute/virtualmachines' -> 'Virtual Machine' (aprox.)."""
    if not target_type:
        return ""
    tail = target_type.split("/")[-1].lower()
    friendly = {
        "virtualmachines": "Virtual Machine",
        "virtualmachinescalesets": "VM Scale Set",
        "managedclusters": "AKS",
        "sites": "App Service",
        "serverfarms": "App Service Plan",
        "servers": "SQL Server",
        "databases": "Azure SQL",
        "storageaccounts": "Storage",
        "applicationgateways": "Application Gateway",
        "loadbalancers": "Load Balancer",
        "components": "App Insights",
        "vaults": "Key Vault / Backup",
        "redis": "Cache Redis",
        "namespaces": "Service Bus / Event Hub",
        "databaseaccounts": "Cosmos DB",
        "functions": "Functions",
        "disks": "Managed Disk",
        "publicipaddresses": "Public IP",
    }
    return friendly.get(tail, tail or target_type)


def _parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        txt = s.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(txt)
    except Exception:  # noqa: BLE001
        return None


def _humanize_delta(ts: Optional[dt.datetime]) -> str:
    """'hace 4 min', 'hace 2 h', 'hace 3 d'. '' si no hay fecha."""
    if ts is None:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    secs = (now - ts).total_seconds()
    if secs < 0:
        return "ahora"
    if secs < 60:
        return "hace segundos"
    mins = secs / 60
    if mins < 60:
        return f"hace {int(mins)} min"
    hrs = mins / 60
    if hrs < 24:
        return f"hace {int(hrs)} h"
    days = hrs / 24
    return f"hace {int(days)} d"


def _duration_str(start: Optional[dt.datetime],
                  end: Optional[dt.datetime]) -> str:
    """Duracion activa: desde el disparo hasta ahora (si Fired) o hasta end."""
    if start is None:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    ref = end or dt.datetime.now(dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    secs = max(0, (ref - start).total_seconds())
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)} min"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)} h {int(mins % 60)} min"
    days = int(hrs // 24)
    return f"{days} d {int(hrs % 24)} h"


COLUMNS = [
    "severity", "severityLabel", "sevRank", "name", "alertState",
    "monitorCondition", "resource", "resourceType", "resourceId",
    "resourceGroup", "subscriptionId", "subscriptionName",
    "signalType", "firedTimes", "lastFired", "lastFiredAgo",
    "firstFired", "duration", "description", "alertRule", "alertId",
    "targetResourceType",
]


def _empty(note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    if note:
        df.attrs["note"] = note
    return df


# ---------------------------------------------------------------------------
# Cliente ARM minimo (reutiliza el pipeline de MonitorManagementClient)
# ---------------------------------------------------------------------------
# Cache de clientes por (id(credential), sub_id): crear el MonitorManagementClient
# arma todo un pipeline (auth, reintentos, etc.) y es costoso. Reutilizarlo entre
# llamadas a la misma suscripcion elimina ese overhead repetido.
_CLIENT_CACHE: Dict[tuple, "MonitorManagementClient"] = {}

import threading as _threading
_CLIENT_LOCK = _threading.Lock()


def _get_client(credential, sub_id: str):
    key = (id(credential), sub_id)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        with _CLIENT_LOCK:
            client = _CLIENT_CACHE.get(key)
            if client is None:
                client = MonitorManagementClient(credential, sub_id)
                _CLIENT_CACHE[key] = client
    return client


def _parse_resp_body(resp) -> dict:
    """Extrae y parsea el JSON del http_response del pipeline de forma robusta
    (RequestsTransportResponse no siempre expone .json())."""
    import json as _json
    raw = None
    for getter in ("text", "body"):
        fn = getattr(resp, getter, None)
        if callable(fn):
            try:
                raw = fn()
                break
            except Exception:  # noqa: BLE001
                continue
    if raw is None:
        raw = getattr(resp, "text", None)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        return {}
    try:
        return _json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _arm_get(credential, sub_id: str, url: str, fresh: bool = False) -> dict:
    """GET autenticado al ARM. Devuelve el JSON (o lanza HttpResponseError).

    fresh=True crea un cliente NUEVO (sin caché compartida). Necesario para
    endpoints como Advisor que, bajo muchas llamadas concurrentes compartiendo
    el mismo cliente/pipeline, pueden fallar con 500.
    """
    client = (MonitorManagementClient(credential, sub_id) if fresh
              else _get_client(credential, sub_id))
    req = client._client.get(url)  # noqa: SLF001
    resp = client._client._pipeline.run(req).http_response  # noqa: SLF001
    status = getattr(resp, "status_code", None)
    if status == 200:
        return _parse_resp_body(resp)
    raise HttpResponseError(response=resp)


def _arm_post(credential, sub_id: str, url: str, body: dict) -> dict:
    """POST autenticado al ARM (JSON). Devuelve el JSON de respuesta.

    sub_id solo se usa para obtener un cliente con pipeline autenticado; el POST
    puede ir a un endpoint global (p. ej. Resource Graph) usando ese pipeline.

    Usa set_json_body(dict) para que el cuerpo viaje como JSON real (no como un
    string doblemente escapado, que Azure rechaza con RequestDeserializationFailure).
    """
    client = _get_client(credential, sub_id)
    req = client._client.post(url)  # noqa: SLF001  (crea el HttpRequest)
    req.set_json_body(body)
    req.headers["Content-Type"] = "application/json"
    resp = client._client._pipeline.run(req).http_response  # noqa: SLF001
    status = getattr(resp, "status_code", None)
    if status == 200:
        return _parse_resp_body(resp)
    raise HttpResponseError(response=resp)


def accessible_subscriptions(credential, sub_ids: list) -> set:
    """Devuelve el conjunto de subscriptionIds a los que el usuario SÍ tiene
    acceso de lectura, consultando Resource Graph una sola vez (ARG solo
    devuelve recursos de subs accesibles). Si una sub no aparece, se asume sin
    acceso. Si ARG falla, devuelve todas (no bloquear por un fallo de ARG)."""
    if not sub_ids:
        return set()
    try:
        url = ("https://management.azure.com/providers/Microsoft.ResourceGraph/"
               "resources?api-version=2024-04-01")
        body = {"subscriptions": list(sub_ids),
                "query": ("ResourceContainers | where type =~ "
                          "'microsoft.resources/subscriptions' "
                          "| project subscriptionId"),
                "options": {"resultFormat": "objectArray", "$top": 1000}}
        payload = _arm_post(credential, sub_ids[0], url, body)
        data = payload.get("data", [])
        if isinstance(data, dict):
            cols = [c["name"] for c in data.get("columns", [])]
            recs = [dict(zip(cols, r)) for r in data.get("rows", [])]
        else:
            recs = data
        found = {r.get("subscriptionId") for r in recs if r.get("subscriptionId")}
        # ResourceContainers de subs puede no listar todas; completar con las que
        # tengan cualquier recurso.
        if not found:
            body["query"] = "Resources | distinct subscriptionId"
            payload = _arm_post(credential, sub_ids[0], url, body)
            data = payload.get("data", [])
            recs = (data if isinstance(data, list)
                    else [dict(zip([c["name"] for c in data.get("columns", [])], r))
                          for r in data.get("rows", [])])
            found = {r.get("subscriptionId") for r in recs if r.get("subscriptionId")}
        return found or set(sub_ids)
    except Exception:  # noqa: BLE001
        return set(sub_ids)


# ---------------------------------------------------------------------------
# Lectura principal de alertas (una suscripcion)
# ---------------------------------------------------------------------------
def list_alerts_sub(credential, sub_id: str, sub_name: str = "",
                    time_range: str = "1d",
                    resource_group: Optional[str] = None) -> pd.DataFrame:
    """Lista las alertas de UNA suscripcion en el rango dado.

    time_range: uno de '1h','1d','7d','30d' (valores aceptados por la API).
    resource_group: filtra por RG si se indica.
    Nunca lanza: ante error de permisos devuelve vacio con nota.
    """
    if not _SDK_OK:
        return _empty("SDK de Azure no disponible.")

    base = (f"/subscriptions/{sub_id}/providers/Microsoft.AlertsManagement/alerts"
            f"?api-version={_API_VERSION}&timeRange={time_range}")
    if resource_group:
        base += f"&targetResourceGroup={resource_group}"

    rows: List[dict] = []
    url = base
    pages = 0
    try:
        while url and pages < 50:
            pages += 1
            payload = _arm_get(credential, sub_id, url)
            for al in payload.get("value", []):
                props = al.get("properties", {}) or {}
                ess = props.get("essentials", {}) or {}
                sev = ess.get("severity", "") or ""
                target_type = ess.get("targetResourceType", "") or ""
                res_id = ess.get("targetResource", "") or ""
                start = _parse_iso(ess.get("startDateTime"))
                last_mod = _parse_iso(ess.get("lastModifiedDateTime"))
                monitor_cond = ess.get("monitorCondition", "") or ""
                alert_state = ess.get("alertState", "") or ""
                rows.append({
                    "severity": sev,
                    "severityLabel": SEV_LABEL.get(sev, sev or "—"),
                    "sevRank": SEV_ORDER.get(sev, 9),
                    "name": al.get("name", "") or ess.get("alertRule", ""),
                    "alertState": alert_state,
                    "monitorCondition": monitor_cond,
                    "resource": _resource_name_from_id(res_id)
                                or ess.get("targetResourceName", ""),
                    "resourceType": _resource_type_pretty(target_type),
                    "targetResourceType": target_type,
                    "resourceId": res_id,
                    "resourceGroup": ess.get("targetResourceGroup", "")
                                     or _rg_from_id(res_id),
                    "subscriptionId": sub_id,
                    "subscriptionName": sub_name or sub_id,
                    "signalType": ess.get("signalType", "") or "",
                    # firedTimes se refina luego (histogram); aqui 1 por alerta.
                    "firedTimes": 1,
                    "lastFired": ess.get("startDateTime", "") or "",
                    "lastFiredAgo": _humanize_delta(start),
                    "firstFired": ess.get("startDateTime", "") or "",
                    "duration": _duration_str(
                        start, None if monitor_cond == "Fired" else last_mod),
                    "description": ess.get("description", "") or "",
                    "alertRule": ess.get("alertRule", "") or "",
                    "alertId": al.get("id", "") or "",
                })
            url = payload.get("nextLink") or ""
        df = pd.DataFrame(rows, columns=COLUMNS)
        return df
    except ClientAuthenticationError as exc:
        return _empty(f"Autenticacion fallida: {exc}")
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return _empty("Sin permisos para leer alertas en esta suscripcion.")
        if code == 404:
            return _empty("AlertsManagement no disponible en la suscripcion.")
        return _empty(f"Error consultando alertas: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _empty(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Multi-suscripcion
# ---------------------------------------------------------------------------
def list_alerts(credential, subscriptions: List[dict],
                time_range: str = "1d",
                resource_group: Optional[str] = None) -> pd.DataFrame:
    """Agrega alertas de varias suscripciones en un solo DataFrame.

    Las suscripciones se consultan EN PARALELO (son I/O de red): con N subs el
    tiempo pasa de ~N*t a ~t. Registra errores por suscripcion en attrs['errors'].
    """
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    frames: List[pd.DataFrame] = []
    errors: List[str] = []

    def _one(sub):
        sid = sub.get("subscriptionId") or sub.get("id") or ""
        name = sub.get("displayName") or sub.get("name") or sid
        df = list_alerts_sub(credential, sid, name, time_range, resource_group)
        return name, df

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

    result = (pd.concat(frames, ignore_index=True)
              if frames else _empty())
    result.attrs["errors"] = errors
    result.attrs["subscriptions_scanned"] = len(subs)
    result.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


# ---------------------------------------------------------------------------
# Resumen agregado (getSummary) — rapido, para KPIs
# ---------------------------------------------------------------------------
def summary_sub(credential, sub_id: str, time_range: str = "1d") -> dict:
    """getSummary agrupado por severidad y estado para UNA suscripcion.

    Devuelve {'by_severity': {sev:count}, 'by_state': {state:count},
    'total': int}. Vacio ante error.
    """
    out = {"by_severity": {}, "by_state": {}, "total": 0}
    if not _SDK_OK:
        return out
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.AlertsManagement/alerts"
           f"/getSummary?api-version={_API_VERSION}"
           f"&groupby=severity,alertState&timeRange={time_range}")
    try:
        payload = _arm_get(credential, sub_id, url)
        props = payload.get("properties", {}) or {}
        out["total"] = int(props.get("total", 0) or 0)
        _walk_summary(props, out)
        return out
    except Exception:  # noqa: BLE001
        return out


def _walk_summary(node: dict, out: dict) -> None:
    """Recorre el arbol de getSummary (groupedby anidado) acumulando conteos."""
    groupedby = node.get("groupedby")
    smart = node.get("smartGroupsCount")
    values = node.get("values") or []
    for v in values:
        name = v.get("name")
        count = int(v.get("count", 0) or 0)
        if groupedby == "severity":
            out["by_severity"][name] = out["by_severity"].get(name, 0) + count
        elif groupedby in ("alertState", "monitorCondition"):
            out["by_state"][name] = out["by_state"].get(name, 0) + count
        # Descender a sub-niveles.
        _walk_summary(v, out)
    _ = smart  # no usado por ahora


def summary(credential, subscriptions: List[dict],
            time_range: str = "1d") -> dict:
    """getSummary agregado sobre varias suscripciones."""
    agg = {"by_severity": {}, "by_state": {}, "total": 0}
    for sub in subscriptions or []:
        sid = sub.get("subscriptionId") or sub.get("id") or ""
        if not sid:
            continue
        s = summary_sub(credential, sid, time_range)
        agg["total"] += s.get("total", 0)
        for k, v in s.get("by_severity", {}).items():
            agg["by_severity"][k] = agg["by_severity"].get(k, 0) + v
        for k, v in s.get("by_state", {}).items():
            agg["by_state"][k] = agg["by_state"].get(k, 0) + v
    return agg


# ---------------------------------------------------------------------------
# Derivados para el dashboard (se calculan sobre el DataFrame de list_alerts)
# ---------------------------------------------------------------------------
def kpis(df: pd.DataFrame) -> dict:
    """KPIs de cabecera. 'disparos' = total de registros del periodo (cada
    alerta devuelta por AlertsManagement representa una instancia disparada)."""
    if df is None or df.empty:
        return {"active": 0, "critical": 0, "warning": 0,
                "informational": 0, "fired": 0}
    fired_mask = df["monitorCondition"].str.lower() == "fired"
    active = int(fired_mask.sum())
    crit = int(((df["severity"] == "Sev0") & fired_mask).sum())
    warn = int(((df["severity"] == "Sev2") & fired_mask).sum())
    info = int(((df["severity"] == "Sev3") & fired_mask).sum())
    return {
        "active": active,
        "critical": crit,
        "warning": warn,
        "informational": info,
        "fired": int(len(df)),  # disparos totales en el periodo
    }


def by_severity(df: pd.DataFrame) -> pd.DataFrame:
    """Conteo por severidad con porcentaje, ordenado por criticidad."""
    cols = ["severity", "severityLabel", "count", "pct"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.groupby(["severity", "severityLabel"]).size()
           .reset_index(name="count"))
    total = g["count"].sum()
    g["pct"] = (g["count"] / total * 100).round(1) if total else 0
    g["rank"] = g["severity"].map(SEV_ORDER).fillna(9)
    g = g.sort_values("rank").drop(columns="rank").reset_index(drop=True)
    return g[cols]


def most_fired(df: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """Reglas de alerta mas disparadas (agrupa por nombre de regla+recurso).

    Añade clasificacion de frecuencia (Alta/Media/Baja) por cuartiles.
    """
    cols = ["name", "severityLabel", "severity", "firedTimes", "resource",
            "resourceType", "alertState", "monitorCondition", "lastFiredAgo",
            "lastFired", "subscriptionName", "resourceGroup", "frequency",
            "alertId", "resourceId"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    key = ["name", "severity", "severityLabel", "resource", "resourceType",
           "subscriptionName", "resourceGroup", "resourceId"]
    g = (df.groupby(key, dropna=False)
           .agg(firedTimes=("alertId", "count"),
                lastFired=("lastFired", "max"),
                monitorCondition=("monitorCondition",
                                  lambda s: "Fired" if (s.str.lower() == "fired").any()
                                  else (s.iloc[0] if len(s) else "")),
                alertState=("alertState",
                            lambda s: s.iloc[0] if len(s) else ""),
                alertId=("alertId", "first"))
           .reset_index())
    g["lastFiredAgo"] = g["lastFired"].map(lambda x: _humanize_delta(_parse_iso(x)))

    # Clasificacion de frecuencia por terciles de firedTimes.
    if len(g) >= 3 and g["firedTimes"].nunique() > 1:
        q1 = g["firedTimes"].quantile(0.5)
        q2 = g["firedTimes"].quantile(0.85)
        def _freq(n):
            if n >= q2:
                return "Alta"
            if n >= q1:
                return "Media"
            return "Baja"
        g["frequency"] = g["firedTimes"].map(_freq)
    else:
        g["frequency"] = g["firedTimes"].map(
            lambda n: "Alta" if n >= 5 else ("Media" if n >= 2 else "Baja"))

    g = g.sort_values("firedTimes", ascending=False).head(top).reset_index(drop=True)
    return g[cols]


def top_resources(df: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """Recursos con mas alertas, con conteo de criticas."""
    cols = ["resource", "resourceType", "resourceGroup", "subscriptionName",
            "alerts", "critical"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["resource"].astype(bool)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.assign(_crit=(df["severity"] == "Sev0").astype(int))
           .groupby(["resource", "resourceType", "resourceGroup",
                     "subscriptionName"], dropna=False)
           .agg(alerts=("alertId", "count"), critical=("_crit", "sum"))
           .reset_index()
           .sort_values(["alerts", "critical"], ascending=[False, False])
           .head(top).reset_index(drop=True))
    return g[cols]


# ---------------------------------------------------------------------------
# Reglas de alerta CONFIGURADAS (todas las creadas, disparadas o no)
# ---------------------------------------------------------------------------
RULE_COLUMNS = [
    "severity", "severityLabel", "sevRank", "name", "alertState",
    "monitorCondition", "resource", "resourceType", "resourceId",
    "resourceGroup", "subscriptionId", "subscriptionName", "signalType",
    "firedTimes", "lastFired", "lastFiredAgo", "firstFired", "duration",
    "description", "alertRule", "alertId", "targetResourceType",
    "ruleKind", "enabled", "condition", "actions", "metricCriteria",
]

# Endpoints de reglas por tipo. (proveedor, api-version, kind legible).
_RULE_ENDPOINTS = [
    ("Microsoft.Insights/metricAlerts", "2018-03-01", "Metric"),
    ("Microsoft.Insights/scheduledQueryRules", "2021-08-01", "Log query"),
    ("Microsoft.Insights/activityLogAlerts", "2020-10-01", "Activity log"),
]


def _sev_from_rule(props: dict, kind: str) -> str:
    """Severidad de la regla -> codigo SevN. metricAlerts/scheduledQueryRules
    exponen 'severity' (0-4). activityLogAlerts no tiene severidad."""
    sev = props.get("severity")
    if sev is None:
        return "Sev3" if kind == "Activity log" else ""
    try:
        return f"Sev{int(sev)}"
    except Exception:  # noqa: BLE001
        return str(sev)


def _rule_scopes(props: dict) -> list:
    """Lista de resourceIds objetivo de la regla (scopes)."""
    scopes = props.get("scopes")
    if isinstance(scopes, list):
        return scopes
    # activityLogAlerts usa 'scopes' tambien; metricAlerts idem.
    return []


# Operadores de Azure -> simbolo legible.
_OP_SYM = {
    "GreaterThan": ">", "GreaterThanOrEqual": ">=",
    "LessThan": "<", "LessThanOrEqual": "<=",
    "Equals": "=", "NotEquals": "!=",
}


def _metric_criteria(props: dict) -> dict:
    """Extrae el primer criterio de METRICA de una regla metricAlert para poder
    graficar la serie temporal: {metricName, aggregation, operator, threshold,
    metricNamespace}. Vacio si no es una alerta de metrica con umbral estatico."""
    crit = props.get("criteria", {}) or {}
    all_of = crit.get("allOf") or []
    for c in all_of:
        metric = c.get("metricName") or c.get("name")
        thr = c.get("threshold")
        if metric and thr is not None:
            return {
                "metricName": metric,
                "aggregation": (c.get("timeAggregation", "") or "Average"),
                "operator": c.get("operator", "GreaterThan"),
                "threshold": float(thr),
                "metricNamespace": c.get("metricNamespace", "") or "",
            }
    return {}


def _iso_duration_to_text(d: Optional[str]) -> str:
    """'PT5M' -> '5 min', 'PT1H' -> '1 h', 'P1D' -> '1 d'. Vacio si no aplica."""
    if not d or not isinstance(d, str):
        return ""
    s = d.upper().lstrip("P")
    is_time = s.startswith("T")
    s = s.lstrip("T")
    out = []
    num = ""
    unit_map = {"D": "d", "H": "h", "M": "min" if is_time else "mes", "S": "s"}
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            if num:
                out.append(f"{num} {unit_map.get(ch, ch)}")
                num = ""
    return " ".join(out) or d


def _condition_text(props: dict, kind: str) -> str:
    """Describe la condicion de disparo en texto legible. '' si no se puede."""
    try:
        if kind == "Metric":
            crit = props.get("criteria", {}) or {}
            all_of = crit.get("allOf") or []
            parts = []
            for c in all_of:
                metric = c.get("metricName", "") or c.get("name", "")
                op = _OP_SYM.get(c.get("operator", ""), c.get("operator", ""))
                thr = c.get("threshold", "")
                agg = c.get("timeAggregation", "")
                piece = f"{metric} {op} {thr}".strip()
                if agg:
                    piece = f"{agg}({metric}) {op} {thr}"
                parts.append(piece)
            win = _iso_duration_to_text(props.get("windowSize"))
            freq = _iso_duration_to_text(props.get("evaluationFrequency"))
            txt = " ; ".join(p for p in parts if p.strip())
            extra = []
            if win:
                extra.append(f"ventana {win}")
            if freq:
                extra.append(f"cada {freq}")
            if extra:
                txt = f"{txt} (" + ", ".join(extra) + ")" if txt else ", ".join(extra)
            return txt
        if kind == "Log query":
            crit = props.get("criteria", {}) or {}
            all_of = crit.get("allOf") or []
            parts = []
            for c in all_of:
                op = _OP_SYM.get(c.get("operator", ""), c.get("operator", ""))
                thr = c.get("threshold", "")
                q = (c.get("query", "") or "")[:80]
                parts.append(f"query [{q}...] {op} {thr}".strip())
            win = _iso_duration_to_text(props.get("windowSize"))
            freq = _iso_duration_to_text(props.get("evaluationFrequency"))
            txt = " ; ".join(parts)
            extra = []
            if win:
                extra.append(f"ventana {win}")
            if freq:
                extra.append(f"cada {freq}")
            if extra:
                txt = f"{txt} (" + ", ".join(extra) + ")" if txt else ", ".join(extra)
            return txt
        if kind == "Activity log":
            cond = props.get("condition", {}) or {}
            all_of = cond.get("allOf") or []
            parts = []
            for c in all_of:
                field = c.get("field", "")
                eq = c.get("equals", "")
                if field and eq:
                    parts.append(f"{field} = {eq}")
            return " ; ".join(parts)
    except Exception:  # noqa: BLE001
        return ""
    return ""


_METRICS_API = "2023-10-01"


def metric_series(credential, sub_id, resource_id, metric_name, aggregation,
                  fired_time=None, hours=6, metric_namespace="") -> pd.DataFrame:
    """Serie temporal de una métrica del recurso alrededor del disparo.

    Devuelve DataFrame [timestamp, value] (vacio si no se puede). Ventana:
    centrada en fired_time ± hours/2 (o las ultimas `hours` si no hay fecha).
    """
    cols = ["timestamp", "value"]
    if not (resource_id and metric_name):
        return pd.DataFrame(columns=cols)
    try:
        fired = _parse_iso_dt(fired_time) if fired_time else None
        now = dt.datetime.now(dt.timezone.utc)
        if fired:
            if fired.tzinfo is None:
                fired = fired.replace(tzinfo=dt.timezone.utc)
            start = fired - dt.timedelta(hours=hours / 2)
            end = min(now, fired + dt.timedelta(hours=hours / 2))
            if end <= start:
                end = start + dt.timedelta(hours=hours)
        else:
            end = now
            start = now - dt.timedelta(hours=hours)
        timespan = f"{start.isoformat()}/{end.isoformat()}"
        agg = (aggregation or "Average")
        url = (f"{resource_id}/providers/Microsoft.Insights/metrics"
               f"?api-version={_METRICS_API}&timespan={timespan}"
               f"&interval=PT5M&metricnames={metric_name}&aggregation={agg}")
        if metric_namespace:
            url += f"&metricnamespace={metric_namespace}"
        # resource_id ya es absoluto; _arm_get antepone el host via pipeline.
        payload = _arm_get(credential, sub_id, url)
        rows = []
        agg_key = agg.lower()
        for m in payload.get("value", []):
            for ts in (m.get("timeseries", []) or []):
                for dp in (ts.get("data", []) or []):
                    val = dp.get(agg_key)
                    if val is None:
                        # algunos vienen con 'average'/'total' aunque se pida otro
                        for k in ("average", "total", "maximum", "minimum", "count"):
                            if dp.get(k) is not None:
                                val = dp.get(k)
                                break
                    if val is not None:
                        rows.append({"timestamp": dp.get("timeStamp"),
                                     "value": round(float(val), 2)})
        return pd.DataFrame(rows, columns=cols)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=cols)


def _parse_iso_dt(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _action_groups_text(props: dict) -> str:
    """Nombres/ids de Action Groups asociados a la regla. '' si no hay."""
    try:
        actions = props.get("actions", {})
        ids = []
        if isinstance(actions, dict):
            for ag in actions.get("actionGroups", []) or []:
                aid = ag.get("actionGroupId") if isinstance(ag, dict) else ag
                if aid:
                    ids.append(aid.rstrip("/").split("/")[-1])
        elif isinstance(actions, list):
            for ag in actions:
                aid = (ag.get("actionGroupId") if isinstance(ag, dict) else ag)
                if aid:
                    ids.append(str(aid).rstrip("/").split("/")[-1])
        return ", ".join(dict.fromkeys(ids))
    except Exception:  # noqa: BLE001
        return ""


def _fetch_rule_endpoint(credential, sub_id, sub_name, provider, api, kind):
    """Consulta UN endpoint de reglas. Devuelve (rows, note|None)."""
    url = f"/subscriptions/{sub_id}/providers/{provider}?api-version={api}"
    rows: List[dict] = []
    try:
        payload = _arm_get(credential, sub_id, url)
        for item in payload.get("value", []):
            props = item.get("properties", {}) or {}
            sev = _sev_from_rule(props, kind)
            scopes = _rule_scopes(props)
            res_id = scopes[0] if scopes else ""
            enabled = props.get("enabled", True)
            rows.append({
                "severity": sev,
                "severityLabel": SEV_LABEL.get(sev, sev or "—"),
                "sevRank": SEV_ORDER.get(sev, 9),
                "name": item.get("name", "") or "",
                "alertState": "Enabled" if enabled else "Disabled",
                "monitorCondition": "",
                "resource": _resource_name_from_id(res_id),
                "resourceType": _resource_type_pretty(
                    res_id.split("/providers/")[-1] if "/providers/" in res_id else ""),
                "targetResourceType": props.get("targetResourceType", "") or "",
                "resourceId": res_id,
                "resourceGroup": _rg_from_id(item.get("id", "")) or _rg_from_id(res_id),
                "subscriptionId": sub_id,
                "subscriptionName": sub_name or sub_id,
                "signalType": kind,
                "firedTimes": 0,
                "lastFired": "",
                "lastFiredAgo": "",
                "firstFired": "",
                "duration": "",
                "description": props.get("description", "") or "",
                "alertRule": item.get("name", "") or "",
                "alertId": item.get("id", "") or "",
                "ruleKind": kind,
                "enabled": bool(enabled),
                "condition": _condition_text(props, kind),
                "actions": _action_groups_text(props),
                "metricCriteria": _metric_criteria(props) if kind == "Metric" else {},
            })
        return rows, None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code not in (401, 403, 404):
            return rows, f"{kind}: {exc}"
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return rows, f"{kind}: {exc}"


def list_alert_rules_sub(credential, sub_id: str, sub_name: str = "") -> pd.DataFrame:
    """Lista TODAS las reglas de alerta CONFIGURADAS en una suscripcion
    (metric, log query y activity log), estén o no disparadas.

    Los 3 endpoints se consultan EN PARALELO. Normaliza al mismo esquema que las
    instancias para poder unirlas. Nunca lanza: ante error devuelve vacio+nota.
    """
    if not _SDK_OK:
        return pd.DataFrame(columns=RULE_COLUMNS)

    rows: List[dict] = []
    notes: List[str] = []
    with ThreadPoolExecutor(max_workers=len(_RULE_ENDPOINTS)) as pool:
        futures = [pool.submit(_fetch_rule_endpoint, credential, sub_id,
                               sub_name, provider, api, kind)
                   for provider, api, kind in _RULE_ENDPOINTS]
        for fut in as_completed(futures):
            r, note = fut.result()
            rows.extend(r)
            if note:
                notes.append(note)

    df = pd.DataFrame(rows, columns=RULE_COLUMNS)
    if notes:
        df.attrs["note"] = " | ".join(notes[:5])
    return df


def list_alert_rules(credential, subscriptions: List[dict]) -> pd.DataFrame:
    """Agrega las reglas configuradas de varias suscripciones EN PARALELO."""
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    frames: List[pd.DataFrame] = []
    errors: List[str] = []

    def _one(sub):
        sid = sub.get("subscriptionId") or sub.get("id") or ""
        name = sub.get("displayName") or sub.get("name") or sid
        return name, list_alert_rules_sub(credential, sid, name)

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
    result = (pd.concat(frames, ignore_index=True)
              if frames else pd.DataFrame(columns=RULE_COLUMNS))
    result.attrs["errors"] = errors
    result.attrs["subscriptions_scanned"] = len(subs)
    result.attrs["fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def merge_rules_and_instances(rules: pd.DataFrame,
                              instances: pd.DataFrame) -> pd.DataFrame:
    """Combina reglas CONFIGURADAS con instancias DISPARADAS.

    - Toda regla aparece (aunque no se haya disparado).
    - Si una regla tiene instancias, hereda firedTimes/lastFired/estado real.
    - Instancias sin regla coincidente (p. ej. alertas de plataforma) tambien
      se incluyen para no perder nada.
    """
    if (rules is None or rules.empty) and (instances is None or instances.empty):
        return pd.DataFrame(columns=RULE_COLUMNS)
    if rules is None or rules.empty:
        out = instances.copy()
        out["ruleKind"] = out.get("signalType", "")
        out["enabled"] = True
        return out
    if instances is None or instances.empty:
        return rules.copy()

    # Agregar instancias por nombre de regla + recurso.
    inst = instances.copy()
    agg = (inst.groupby(["name", "resourceId"], dropna=False)
               .agg(firedTimes=("alertId", "count"),
                    lastFired=("lastFired", "max"),
                    monitorCondition=("monitorCondition",
                                      lambda s: "Fired" if (s.str.lower() == "fired").any()
                                      else (s.iloc[0] if len(s) else "")))
               .reset_index())

    merged = rules.merge(agg, on=["name", "resourceId"], how="left",
                         suffixes=("", "_inst"))
    # Volcar los valores de instancias donde existan.
    merged["firedTimes"] = merged["firedTimes_inst"].fillna(0).astype(int)
    merged["lastFired"] = merged["lastFired_inst"].fillna(merged["lastFired"])
    merged["monitorCondition"] = merged["monitorCondition_inst"].fillna(
        merged["monitorCondition"]).fillna("")
    merged["lastFiredAgo"] = merged["lastFired"].map(
        lambda x: _humanize_delta(_parse_iso(x)) if x else "")
    merged = merged.drop(columns=[c for c in merged.columns
                                  if c.endswith("_inst")])

    # Instancias que no matchearon ninguna regla -> añadirlas.
    rule_keys = set(zip(rules["name"], rules["resourceId"]))
    extra = inst[~inst.apply(lambda r: (r["name"], r["resourceId"]) in rule_keys,
                             axis=1)].copy()
    if not extra.empty:
        extra["ruleKind"] = extra.get("signalType", "")
        extra["enabled"] = True
        merged = pd.concat([merged, extra], ignore_index=True)

    # Asegurar todas las columnas del esquema de reglas.
    for c in RULE_COLUMNS:
        if c not in merged.columns:
            merged[c] = "" if c not in ("firedTimes", "sevRank") else 0
    return merged[RULE_COLUMNS]


def resource_groups(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty or "resourceGroup" not in df.columns:
        return []
    vals = sorted({r for r in df["resourceGroup"].dropna().tolist() if r})
    return vals


def resource_types(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty or "resourceType" not in df.columns:
        return []
    return sorted({r for r in df["resourceType"].dropna().tolist() if r})
