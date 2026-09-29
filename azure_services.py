"""
azure_services.py
=================
Lógica de conexión, descubrimiento, extracción y remediación para Azure,
organizada por niveles operativos del PDM (L1/L2/L3).

Autenticación:
    - InteractiveBrowserCredential: abre el navegador para login interactivo
      (usado en la pantalla inicial cuando no hay sesión previa).
    - DefaultAzureCredential: reutiliza 'az login' si existe.
    - ClientSecretCredential: Service Principal (automatización).

Descubrimiento:
    SubscriptionClient lista automáticamente todas las suscripciones
    accesibles para el usuario autenticado.

Lecturas devuelven pandas.DataFrame; acciones L2 devuelven dict
{"ok": bool, "message": str}.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import pandas as pd
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    InteractiveBrowserCredential,
)
from azure.mgmt.advisor import AdvisorManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient, SubscriptionClient
from azure.mgmt.security import SecurityCenter


class AzureAuthError(Exception):
    """Se lanza cuando la credencial de Azure no es válida o expiró."""


# ===========================================================================
# Autenticación y descubrimiento
# ===========================================================================
def get_credential(mode: str = "interactive", tenant_id: Optional[str] = None,
                   client_id: Optional[str] = None,
                   client_secret: Optional[str] = None):
    """
    mode:
        "interactive" -> InteractiveBrowserCredential (abre navegador).
        "default"     -> DefaultAzureCredential (reutiliza 'az login').
        "cli"         -> AzureCliCredential.
        "sp"          -> ClientSecretCredential (Service Principal).
    """
    try:
        if mode == "sp":
            if not all([tenant_id, client_id, client_secret]):
                raise AzureAuthError(
                    "Service Principal requiere tenant_id, client_id y client_secret."
                )
            return ClientSecretCredential(tenant_id, client_id, client_secret)
        if mode == "cli":
            return AzureCliCredential()
        if mode == "default":
            return DefaultAzureCredential(exclude_interactive_browser_credential=False)
        # interactive (por defecto en pantalla inicial)
        kwargs = {}
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        return InteractiveBrowserCredential(**kwargs)
    except ClientAuthenticationError as exc:
        raise AzureAuthError(
            f"No se pudo obtener la credencial de Azure. Detalle: {exc}"
        ) from exc


def discover_subscriptions(credential) -> pd.DataFrame:
    """Lista todas las suscripciones accesibles para el usuario."""
    columns = ["subscriptionId", "displayName", "state", "tenantId"]
    try:
        client = SubscriptionClient(credential)
        rows = []
        for s in client.subscriptions.list():
            rows.append({
                "subscriptionId": s.subscription_id,
                "displayName": s.display_name,
                "state": str(s.state),
                "tenantId": getattr(s, "tenant_id", None),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        raise


def discover_tenants(credential) -> pd.DataFrame:
    """
    Lista los tenants (directorios) a los que el usuario tiene acceso.
    Útil cuando subscriptions.list() vuelve vacío: normalmente significa que
    la suscripción vive en OTRO tenant distinto al del login por defecto.
    """
    columns = ["tenantId", "defaultDomain", "displayName"]
    try:
        client = SubscriptionClient(credential)
        rows = []
        for t in client.tenants.list():
            rows.append({
                "tenantId": t.tenant_id,
                "defaultDomain": getattr(t, "default_domain", None),
                "displayName": getattr(t, "display_name", None),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        raise


# ===========================================================================
# Ejecución consolidada multi-suscripción
# ===========================================================================
def run_all_subscriptions(func, credential, subscriptions, *args, **kwargs) -> pd.DataFrame:
    """
    Ejecuta una función L1/L3 (que recibe credential + subscription_id) sobre
    TODAS las suscripciones indicadas y concatena los resultados, anteponiendo
    las columnas 'subscriptionName' y 'subscriptionId' para saber a qué sub
    pertenece cada hallazgo.

    Parámetros:
        func          : callable(credential, subscription_id, *args, **kwargs) -> DataFrame
        credential    : credencial de Azure ya autenticada.
        subscriptions : lista de dicts [{"subscriptionId":..., "displayName":...}, ...]
                        o DataFrame con columnas subscriptionId/displayName.

    Devuelve un único DataFrame consolidado. Los errores por suscripción NO
    detienen el proceso: se registran en df.attrs['errors'].
    Reeleva AzureAuthError (token expirado) para que la UI pida re-login.
    """
    # Normalizamos la lista de suscripciones a dicts
    if isinstance(subscriptions, pd.DataFrame):
        subs = subscriptions.to_dict("records")
    else:
        subs = list(subscriptions)

    frames: List[pd.DataFrame] = []
    errors: List[str] = []
    notes: List[str] = []
    auth_failure: List[Exception] = []

    def _work(sub):
        sub_id = sub.get("subscriptionId")
        sub_name = sub.get("displayName") or sub_id
        try:
            df = func(credential, sub_id, *args, **kwargs)
            return ("ok", sub_id, sub_name, df)
        except AzureAuthError as exc:
            return ("auth", sub_id, sub_name, exc)
        except Exception as exc:  # noqa: BLE001
            return ("err", sub_id, sub_name, exc)

    # Ejecución EN PARALELO (segundo plano) para no demorar con muchas subs.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = min(12, max(1, len(subs)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_work, s) for s in subs]
        for fut in as_completed(futures):
            kind, sub_id, sub_name, payload = fut.result()
            if kind == "auth":
                auth_failure.append(payload)
                continue
            if kind == "err":
                errors.append(f"{sub_name} ({sub_id}): {payload}")
                continue
            df = payload if payload is not None else pd.DataFrame()
            note = df.attrs.get("note") if hasattr(df, "attrs") else None
            if note:
                notes.append(f"{sub_name}: {note}")
            if not df.empty:
                df = df.copy()
                df.insert(0, "subscriptionName", sub_name)
                df.insert(1, "subscriptionId", sub_id)
                frames.append(df)

    # Si el token expiró en alguna llamada, propagamos para pedir re-login.
    if auth_failure and not frames:
        raise auth_failure[0]

    if frames:
        result = pd.concat(frames, ignore_index=True)
    else:
        # Sin resultados: devolvemos DataFrame vacío conservando el esquema
        base_cols = ["subscriptionName", "subscriptionId"]
        result = pd.DataFrame(columns=base_cols)

    if errors:
        result.attrs["errors"] = errors
    if notes:
        result.attrs["note"] = " | ".join(notes[:5]) + (
            f" (+{len(notes) - 5} más)" if len(notes) > 5 else ""
        )
    result.attrs["subscriptions_scanned"] = len(subs)
    return result


# ===========================================================================
# Utilidades internas
# ===========================================================================
def _raise_auth(exc) -> None:
    raise AzureAuthError(
        "Autenticación de Azure fallida o expirada. Vuelve a iniciar sesión. "
        f"Detalle: {exc}"
    ) from exc


def _raise_if_auth(exc: HttpResponseError) -> None:
    if getattr(exc, "status_code", None) == 401:
        raise AzureAuthError(
            "Token de Azure expirado o inválido (401). Vuelve a iniciar sesión."
        ) from exc


def _rg_from_id(resource_id: Optional[str]) -> Optional[str]:
    if not resource_id:
        return None
    parts = resource_id.split("/")
    try:
        idx = [p.lower() for p in parts].index("resourcegroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def _fmt(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _fmt_cost_date(value):
    if value is None:
        return None
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _empty_with_note(columns: List[str], note: str) -> pd.DataFrame:
    df = pd.DataFrame(columns=columns)
    df.attrs["note"] = note
    return df


# ===========================================================================
# NIVEL 1 (L1) — Monitoreo (solo lectura)
# ===========================================================================

# ---- 1. Estado de Salud ----------------------------------------------------
def l1_service_health(credential, subscription_id: str) -> pd.DataFrame:
    columns = ["name", "eventType", "status", "title", "impactStartTime",
               "lastUpdateTime", "trackingId"]
    try:
        client = ResourceManagementClient(credential, subscription_id)
        rows = _list_health_events(client, subscription_id)
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Service Health no disponible o sin permisos en esta suscripción.")
        raise


def _list_health_events(resource_client, subscription_id: str) -> List[dict]:
    rows: List[dict] = []
    try:
        api_version = "2022-10-01"
        url = (
            f"/subscriptions/{subscription_id}/providers/"
            f"Microsoft.ResourceHealth/events?api-version={api_version}"
            f"&$filter=eventType eq 'ServiceIssue' and status eq 'Active'"
        )
        request = resource_client._client.get(url)  # noqa: SLF001
        response = resource_client._client._pipeline.run(request)  # noqa: SLF001
        http_resp = response.http_response
        if http_resp.status_code == 200:
            for ev in http_resp.json().get("value", []):
                props = ev.get("properties", {})
                rows.append({
                    "name": ev.get("name"),
                    "eventType": props.get("eventType"),
                    "status": props.get("status"),
                    "title": props.get("title"),
                    "impactStartTime": props.get("impactStartTime"),
                    "lastUpdateTime": props.get("lastUpdateTime"),
                    "trackingId": props.get("trackingId"),
                })
    except Exception:  # noqa: BLE001
        pass
    return rows


# ---- 2. Costos --------------------------------------------------------------
def l1_cost_last_7_days(credential, subscription_id: str) -> pd.DataFrame:
    return _query_cost(credential, subscription_id, days=7, granularity="Daily")


def l1_cost_current_month(credential, subscription_id: str) -> pd.DataFrame:
    today = dt.date.today()
    return _query_cost(credential, subscription_id, start=today.replace(day=1),
                       end=today, granularity="None")


def _query_cost(credential, subscription_id: str, days: Optional[int] = None,
                start: Optional[dt.date] = None, end: Optional[dt.date] = None,
                granularity: str = "Daily") -> pd.DataFrame:
    columns = ["date", "service", "amount", "currency"]
    try:
        client = CostManagementClient(credential)
        scope = f"/subscriptions/{subscription_id}"
        if days is not None:
            end = dt.date.today()
            start = end - dt.timedelta(days=days)
        query = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "time_period": {
                "from_property": dt.datetime.combine(start, dt.time.min).isoformat() + "Z",
                "to": dt.datetime.combine(end, dt.time.max).isoformat() + "Z",
            },
            "dataset": {
                "granularity": granularity,
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ServiceName"}],
            },
        }
        result = client.query.usage(scope=scope, parameters=query)
        cols = [c.name for c in result.columns]
        rows = []
        for row in result.rows:
            rec = dict(zip(cols, row))
            rows.append({
                "date": _fmt_cost_date(rec.get("UsageDate")),
                "service": rec.get("ServiceName"),
                "amount": round(float(rec.get("Cost", 0) or 0), 4),
                "currency": rec.get("Currency"),
            })
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            df = df.sort_values(["date", "amount"], ascending=[True, False])
        return df.reset_index(drop=True)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Cost Management no accesible (permisos o suscripción sin datos).")
        raise


# ---- 3. Recursos Huérfanos --------------------------------------------------
def l1_orphaned_resources(credential, subscription_id: str) -> pd.DataFrame:
    """
    Discos 'Unattached', IPs públicas sin asociación y Load Balancers sin
    backend pools con miembros.
    """
    columns = ["type", "name", "resourceGroup", "location", "detail", "resourceId"]
    rows: List[dict] = []
    try:
        compute = ComputeManagementClient(credential, subscription_id)
        for disk in compute.disks.list():
            if (disk.disk_state or "").lower() == "unattached":
                rows.append({
                    "type": "ManagedDisk (Unattached)",
                    "name": disk.name,
                    "resourceGroup": _rg_from_id(disk.id),
                    "location": disk.location,
                    "detail": f"{disk.disk_size_gb} GB / {disk.sku.name if disk.sku else 'n/a'}",
                    "resourceId": disk.id,
                })

        network = NetworkManagementClient(credential, subscription_id)
        for pip in network.public_ip_addresses.list_all():
            if pip.ip_configuration is None:
                rows.append({
                    "type": "PublicIP (Unassociated)",
                    "name": pip.name,
                    "resourceGroup": _rg_from_id(pip.id),
                    "location": pip.location,
                    "detail": f"{pip.ip_address or 'sin IP'} / {pip.sku.name if pip.sku else 'n/a'}",
                    "resourceId": pip.id,
                })

        for lb in network.load_balancers.list_all():
            pools = lb.backend_address_pools or []
            has_members = any(
                (getattr(p, "backend_ip_configurations", None) or
                 getattr(p, "load_balancer_backend_addresses", None))
                for p in pools
            )
            if not pools or not has_members:
                rows.append({
                    "type": "LoadBalancer (inactivo)",
                    "name": lb.name,
                    "resourceGroup": _rg_from_id(lb.id),
                    "location": lb.location,
                    "detail": f"backend_pools={len(pools)}",
                    "resourceId": lb.id,
                })

        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Sin permisos para listar recursos en esta suscripción.")
        raise


# ---- 4. Servicios Deprecados / Retiros -------------------------------------
def l1_service_retirements(credential, subscription_id: str) -> pd.DataFrame:
    columns = ["category", "impact", "problem", "solution", "resourceId"]
    try:
        client = AdvisorManagementClient(credential, subscription_id)
        rows = []
        keywords = ("retire", "retirement", "deprecat", "end of life", "eol",
                    "upgrade", "no longer", "unsupported", "migrate")
        for rec in client.recommendations.list():
            short = getattr(rec, "short_description", None)
            problem = getattr(short, "problem", "") if short else ""
            solution = getattr(short, "solution", "") if short else ""
            if any(k in f"{problem} {solution}".lower() for k in keywords):
                meta = getattr(rec, "resource_metadata", None)
                rows.append({
                    "category": getattr(rec, "category", None),
                    "impact": getattr(rec, "impact", None),
                    "problem": problem,
                    "solution": solution,
                    "resourceId": meta.resource_id if meta else getattr(rec, "id", None),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns, "Azure Advisor no accesible.")
        raise


# ---- 5. Seguridad y Alertas -------------------------------------------------
def l1_security_alerts(credential, subscription_id: str) -> pd.DataFrame:
    columns = ["alertDisplayName", "severity", "status", "startTime",
               "resourceIdentifiers", "alertName"]
    try:
        client = SecurityCenter(credential, subscription_id)
        rows = []
        for alert in client.alerts.list():
            severity = (getattr(alert, "severity", "") or "")
            if str(severity).lower() not in ("high", "medium", "critical"):
                continue
            res_ids = getattr(alert, "resource_identifiers", None) or []
            res_str = ", ".join(
                [getattr(r, "azure_resource_id", None) or getattr(r, "type", "") for r in res_ids]
            )
            rows.append({
                "alertDisplayName": getattr(alert, "alert_display_name", None),
                "severity": severity,
                "status": getattr(alert, "status", None),
                "startTime": _fmt(getattr(alert, "start_time_utc", None)),
                "resourceIdentifiers": res_str,
                "alertName": getattr(alert, "name", None),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Defender for Cloud (alertas) no accesible en esta suscripción.")
        raise


def l1_secure_score(credential, subscription_id: str) -> pd.DataFrame:
    columns = ["name", "currentScore", "maxScore", "percentage", "weight"]
    try:
        client = SecurityCenter(credential, subscription_id)
        rows = []
        for sc in client.secure_scores.list():
            score = getattr(sc, "score", None)
            pct = getattr(score, "percentage", None)
            rows.append({
                "name": getattr(sc, "display_name", None) or getattr(sc, "name", None),
                "currentScore": getattr(score, "current", None),
                "maxScore": getattr(score, "max", None),
                "percentage": round(float(pct) * 100, 2) if pct is not None else None,
                "weight": getattr(sc, "weight", None),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Defender for Cloud (Secure Score) no accesible.")
        raise


def l1_monitor_alerts(credential, subscription_id: str) -> pd.DataFrame:
    columns = ["name", "severity", "alertState", "monitorCondition",
               "targetResource", "firedTime"]
    try:
        client = MonitorManagementClient(credential, subscription_id)
        rows = _list_monitor_alerts(client, subscription_id)
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns, "Azure Monitor Alerts no accesible.")
        raise


def _list_monitor_alerts(monitor_client, subscription_id: str) -> List[dict]:
    rows: List[dict] = []
    api_version = "2019-05-05-preview"
    for sev in ("Sev0", "Sev1", "Sev2"):
        try:
            url = (
                f"/subscriptions/{subscription_id}/providers/"
                f"Microsoft.AlertsManagement/alerts?api-version={api_version}"
                f"&severity={sev}&monitorCondition=Fired"
            )
            request = monitor_client._client.get(url)  # noqa: SLF001
            response = monitor_client._client._pipeline.run(request)  # noqa: SLF001
            http_resp = response.http_response
            if http_resp.status_code != 200:
                continue
            for al in http_resp.json().get("value", []):
                ess = al.get("properties", {}).get("essentials", {})
                rows.append({
                    "name": al.get("name"),
                    "severity": ess.get("severity"),
                    "alertState": ess.get("alertState"),
                    "monitorCondition": ess.get("monitorCondition"),
                    "targetResource": ess.get("targetResource"),
                    "firedTime": ess.get("startDateTime"),
                })
        except Exception:  # noqa: BLE001
            continue
    return rows


# ===========================================================================
# NIVEL 2 (L2) — Administración / Remediación (ACCIONES DESTRUCTIVAS)
# ===========================================================================
def _parse_resource_id(resource_id: str) -> Dict[str, Optional[str]]:
    """Extrae resourceGroup y nombre del recurso desde su Resource ID."""
    parts = resource_id.split("/")
    lower = [p.lower() for p in parts]
    rg = None
    name = parts[-1] if parts else None
    try:
        rg = parts[lower.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        pass
    return {"resourceGroup": rg, "name": name}


def l2_delete_disk(credential, subscription_id: str, resource_id: str) -> Dict:
    """Elimina un Managed Disk (debe estar Unattached)."""
    try:
        info = _parse_resource_id(resource_id)
        client = ComputeManagementClient(credential, subscription_id)
        poller = client.disks.begin_delete(info["resourceGroup"], info["name"])
        poller.result()
        return {"ok": True, "message": f"Disco '{info['name']}' eliminado."}
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo eliminar el disco: {exc}"}


def l2_delete_public_ip(credential, subscription_id: str, resource_id: str) -> Dict:
    """Libera/elimina una Public IP no asociada."""
    try:
        info = _parse_resource_id(resource_id)
        client = NetworkManagementClient(credential, subscription_id)
        poller = client.public_ip_addresses.begin_delete(info["resourceGroup"], info["name"])
        poller.result()
        return {"ok": True, "message": f"Public IP '{info['name']}' liberada."}
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo liberar la IP: {exc}"}


def l2_delete_load_balancer(credential, subscription_id: str, resource_id: str) -> Dict:
    """Elimina un Load Balancer inactivo."""
    try:
        info = _parse_resource_id(resource_id)
        client = NetworkManagementClient(credential, subscription_id)
        poller = client.load_balancers.begin_delete(info["resourceGroup"], info["name"])
        poller.result()
        return {"ok": True, "message": f"Load Balancer '{info['name']}' eliminado."}
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo eliminar el LB: {exc}"}


def l2_dismiss_security_alert(credential, subscription_id: str,
                              alert_name: str, location: str = "centralus") -> Dict:
    """
    Remediación de seguridad: cambia el estado de una alerta de Defender for
    Cloud a 'Dismiss'. La ubicación es el ASC location de la suscripción.
    """
    try:
        client = SecurityCenter(credential, subscription_id)
        # update_subscription_level_state_to_dismiss requiere el location del ASC
        client.alerts.update_subscription_level_state_to_dismiss(location, alert_name)
        return {"ok": True, "message": f"Alerta '{alert_name}' descartada."}
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo actualizar la alerta: {exc}"}


# ===========================================================================
# NIVEL 3 (L3) — Arquitectura / Gobierno / Optimización
# ===========================================================================
def l3_rightsizing_recommendations(credential, subscription_id: str) -> pd.DataFrame:
    """
    Recomendaciones de right-sizing y ahorro de Azure Advisor
    (categorías Cost y Performance).
    """
    columns = ["category", "impact", "problem", "solution", "resourceId",
               "savings"]
    try:
        client = AdvisorManagementClient(credential, subscription_id)
        rows = []
        for rec in client.recommendations.list():
            category = str(getattr(rec, "category", "") or "")
            if category.lower() not in ("cost", "performance"):
                continue
            short = getattr(rec, "short_description", None)
            meta = getattr(rec, "resource_metadata", None)
            ext = getattr(rec, "extended_properties", None) or {}
            savings = ext.get("savingsAmount") or ext.get("annualSavingsAmount")
            rows.append({
                "category": category,
                "impact": getattr(rec, "impact", None),
                "problem": getattr(short, "problem", "") if short else "",
                "solution": getattr(short, "solution", "") if short else "",
                "resourceId": meta.resource_id if meta else getattr(rec, "id", None),
                "savings": savings,
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns, "Azure Advisor no accesible.")
        raise


def l3_cost_by_service_month(credential, subscription_id: str) -> pd.DataFrame:
    """Gobierno de costos: gasto mensual por servicio (dashboards tipo CUDOS)."""
    return l1_cost_current_month(credential, subscription_id)


def l3_well_architected_security(credential, subscription_id: str) -> pd.DataFrame:
    """Postura avanzada: Secure Score por control de Defender for Cloud."""
    columns = ["control", "currentScore", "maxScore", "healthyResources",
               "unhealthyResources"]
    try:
        client = SecurityCenter(credential, subscription_id)
        rows = []
        for ctl in client.secure_score_controls.list():
            score = getattr(ctl, "score", None)
            rows.append({
                "control": getattr(ctl, "display_name", None) or getattr(ctl, "name", None),
                "currentScore": getattr(score, "current", None),
                "maxScore": getattr(score, "max", None),
                "healthyResources": getattr(ctl, "healthy_resource_count", None),
                "unhealthyResources": getattr(ctl, "unhealthy_resource_count", None),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                "Defender for Cloud (controles) no accesible.")
        raise


def l3_service_quotas(credential, subscription_id: str,
                      location: str = "eastus") -> pd.DataFrame:
    """
    Límites/cuotas de cómputo en una región: uso actual vs límite para
    planificación de capacidad. Marca las cuotas por encima del 80%.
    """
    columns = ["name", "currentValue", "limit", "usagePct", "unit"]
    try:
        client = ComputeManagementClient(credential, subscription_id)
        rows = []
        for u in client.usage.list(location):
            name = getattr(u.name, "localized_value", None) or getattr(u.name, "value", None)
            current = u.current_value
            limit = u.limit
            pct = round((current / limit) * 100, 2) if limit else None
            rows.append({
                "name": name,
                "currentValue": current,
                "limit": limit,
                "usagePct": pct,
                "unit": str(u.unit),
            })
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            df = df.sort_values("usagePct", ascending=False, na_position="last")
        return df.reset_index(drop=True)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns,
                f"No se pudieron leer cuotas de cómputo en {location}.")
        raise


# ===========================================================================
# EXTENSIONES PDM — Backups, Updates, Reservas
# ===========================================================================

# ---- L1 · 1.7 Respaldo de Servicios (Recovery Services Vaults) -------------
def l1_backup_status(credential, subscription_id: str) -> pd.DataFrame:
    """
    Inventario de Recovery Services Vaults (respaldo). Como indicador de
    respaldo, lista los vaults existentes por grupo de recursos.
    """
    columns = ["vaultName", "resourceGroup", "location", "sku"]
    try:
        client = ResourceManagementClient(credential, subscription_id)
        rows = []
        vaults = client.resources.list(
            filter="resourceType eq 'Microsoft.RecoveryServices/vaults'"
        )
        for v in vaults:
            rows.append({
                "vaultName": v.name,
                "resourceGroup": _rg_from_id(v.id),
                "location": v.location,
                "sku": getattr(v.sku, "name", None) if getattr(v, "sku", None) else None,
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns, "Sin permisos para Recovery Services.")
        raise


# ---- L2 · 1.2 Upgrades y Parches (Update Management / Advisor) -------------
def l2_available_updates(credential, subscription_id: str) -> pd.DataFrame:
    """
    Recomendaciones de Advisor de categoría 'HighAvailability' y
    'OperationalExcellence' relacionadas con actualizaciones/parches.
    """
    columns = ["category", "impact", "problem", "solution", "resourceId"]
    try:
        client = AdvisorManagementClient(credential, subscription_id)
        rows = []
        keywords = ("update", "patch", "upgrade", "version", "outdated",
                    "latest", "os ", "operating system")
        for rec in client.recommendations.list():
            short = getattr(rec, "short_description", None)
            problem = getattr(short, "problem", "") if short else ""
            solution = getattr(short, "solution", "") if short else ""
            if any(k in f"{problem} {solution}".lower() for k in keywords):
                meta = getattr(rec, "resource_metadata", None)
                rows.append({
                    "category": getattr(rec, "category", None),
                    "impact": getattr(rec, "impact", None),
                    "problem": problem,
                    "solution": solution,
                    "resourceId": meta.resource_id if meta else getattr(rec, "id", None),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        if exc.status_code in (403, 404):
            return _empty_with_note(columns, "Azure Advisor no accesible.")
        raise


# ---- L3 · Proyecciones financieras (Reservas) ------------------------------
def l3_reservation_recommendations(credential, subscription_id: str) -> pd.DataFrame:
    """
    Recomendaciones de compra de reservas (Reserved Instances) vía la API REST
    de Consumption. Best-effort.
    """
    columns = ["scope", "resourceType", "term", "recommendedQuantity",
               "estimatedSavings", "meterId"]
    rows: List[dict] = []
    try:
        client = ResourceManagementClient(credential, subscription_id)
        api_version = "2023-05-01"
        url = (
            f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/"
            f"reservationRecommendations?api-version={api_version}"
        )
        request = client._client.get(url)  # noqa: SLF001
        response = client._client._pipeline.run(request)  # noqa: SLF001
        http_resp = response.http_response
        if http_resp.status_code == 200:
            for r in http_resp.json().get("value", []):
                props = r.get("properties", {})
                rows.append({
                    "scope": props.get("scope"),
                    "resourceType": props.get("resourceType"),
                    "term": props.get("term"),
                    "recommendedQuantity": props.get("recommendedQuantity"),
                    "estimatedSavings": props.get("netSavings"),
                    "meterId": props.get("meterId"),
                })
        df = pd.DataFrame(rows, columns=columns)
        if df.empty:
            return _empty_with_note(columns,
                "Sin recomendaciones de reservas o sin permisos de Consumption.")
        return df
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"Reservas no accesibles: {exc}")


# ---- L3 · Playbooks de automatización (Automation Accounts / Runbooks) -----
def l3_automation_playbooks(credential, subscription_id: str) -> pd.DataFrame:
    """Cuentas de automatización de Azure (contenedores de runbooks/playbooks)."""
    columns = ["name", "resourceGroup", "location", "type"]
    try:
        client = ResourceManagementClient(credential, subscription_id)
        rows = []
        accounts = client.resources.list(
            filter="resourceType eq 'Microsoft.Automation/automationAccounts'"
        )
        for a in accounts:
            rows.append({
                "name": a.name,
                "resourceGroup": _rg_from_id(a.id),
                "location": a.location,
                "type": a.type,
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"Automation Accounts no accesibles: {exc}")
