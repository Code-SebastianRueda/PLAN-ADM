"""
azure_core.py
=============
Lógica de conexión, descubrimiento, extracción y acciones para Azure.

Autenticación:
    - InteractiveBrowserCredential (login en navegador, pantalla inicial).
    - DefaultAzureCredential / AzureCliCredential / ClientSecretCredential.

Descubrimiento multi-suscripción:
    run_all(func, credential, subs) ejecuta func en paralelo sobre cada
    suscripción y concatena resultados, añadiendo subscriptionName/Id.

Lecturas   -> pandas.DataFrame
Acciones   -> dict {"ok": bool, "message": str}
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ── Excepciones de autenticación ─────────────────────────────────────────────
class AzureAuthError(Exception):
    pass


def _raise_auth(exc) -> None:
    raise AzureAuthError(
        f"Autenticación de Azure fallida o expirada. Vuelve a hacer login. Detalle: {exc}"
    ) from exc


def _guard_http(exc: HttpResponseError) -> None:
    if getattr(exc, "status_code", None) == 401:
        raise AzureAuthError("Token expirado (401). Vuelve a hacer login.") from exc


def _fmt(v):
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


def _fmt_date(v):
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _rg(resource_id: Optional[str]) -> Optional[str]:
    if not resource_id:
        return None
    parts = resource_id.split("/")
    try:
        return parts[[p.lower() for p in parts].index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return None


def _empty(cols: List[str], note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=cols)
    if note:
        df.attrs["note"] = note
    return df


# ── Autenticación ─────────────────────────────────────────────────────────────
def get_credential(mode: str = "interactive", tenant_id: Optional[str] = None,
                   client_id: Optional[str] = None, client_secret: Optional[str] = None):
    try:
        if mode == "sp":
            if not all([tenant_id, client_id, client_secret]):
                raise AzureAuthError("Service Principal requiere tenant_id, client_id y client_secret.")
            return ClientSecretCredential(tenant_id, client_id, client_secret)
        if mode == "cli":
            return AzureCliCredential()
        if mode == "default":
            return DefaultAzureCredential(exclude_interactive_browser_credential=False)
        return InteractiveBrowserCredential(**({"tenant_id": tenant_id} if tenant_id else {}))
    except ClientAuthenticationError as exc:
        raise AzureAuthError(f"No se pudo obtener credencial. Detalle: {exc}") from exc


# ── Descubrimiento ─────────────────────────────────────────────────────────────
def discover_subscriptions(cred) -> pd.DataFrame:
    cols = ["subscriptionId", "displayName", "state", "tenantId"]
    try:
        client = SubscriptionClient(cred)
        rows = [{"subscriptionId": s.subscription_id, "displayName": s.display_name,
                 "state": str(s.state), "tenantId": getattr(s, "tenant_id", None)}
                for s in client.subscriptions.list()]
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        raise


def discover_tenants(cred) -> pd.DataFrame:
    cols = ["tenantId", "defaultDomain", "displayName"]
    try:
        client = SubscriptionClient(cred)
        rows = [{"tenantId": t.tenant_id, "defaultDomain": getattr(t, "default_domain", None),
                 "displayName": getattr(t, "display_name", None)}
                for t in client.tenants.list()]
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        raise


# ── Ejecución multi-suscripción en paralelo ───────────────────────────────────
def run_all(func, cred, subs, *args, **kwargs) -> pd.DataFrame:
    """
    Ejecuta func(cred, sub_id, *args, **kwargs) en paralelo sobre cada sub.
    Antepone subscriptionName + subscriptionId a cada fila. Tolera errores
    por sub (los registra en df.attrs['errors']). Propaga AzureAuthError.
    """
    if isinstance(subs, pd.DataFrame):
        subs = subs.to_dict("records")

    frames: List[pd.DataFrame] = []
    errors: List[str] = []
    notes: List[str] = []
    auth_err = []

    def _work(sub):
        sid = sub.get("subscriptionId")
        name = sub.get("displayName") or sid
        try:
            df = func(cred, sid, *args, **kwargs) or pd.DataFrame()
            return "ok", sid, name, df
        except AzureAuthError as exc:
            return "auth", sid, name, exc
        except Exception as exc:  # noqa: BLE001
            return "err", sid, name, exc

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(subs)))) as pool:
        futures = [pool.submit(_work, s) for s in subs]
        for fut in as_completed(futures):
            kind, sid, name, payload = fut.result()
            if kind == "auth":
                auth_err.append(payload)
            elif kind == "err":
                errors.append(f"{name}: {payload}")
            else:
                note = payload.attrs.get("note") if hasattr(payload, "attrs") else None
                if note:
                    notes.append(f"{name}: {note}")
                if not payload.empty:
                    df = payload.copy()
                    df.insert(0, "subscriptionName", name)
                    df.insert(1, "subscriptionId", sid)
                    frames.append(df)

    if auth_err and not frames:
        raise auth_err[0]

    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result.attrs["errors"] = errors
    result.attrs["subscriptions_scanned"] = len(subs)
    if notes:
        result.attrs["note"] = " | ".join(notes[:5])
    return result


# ════════════════════════════════════════════════════════════════════════════
# 1. SALUD
# ════════════════════════════════════════════════════════════════════════════
def health_events(cred, sub_id: str) -> pd.DataFrame:
    cols = ["name", "eventType", "status", "title", "impactStartTime",
            "lastUpdateTime", "trackingId"]
    try:
        client = ResourceManagementClient(cred, sub_id)
        rows: List[dict] = []
        url = (f"/subscriptions/{sub_id}/providers/Microsoft.ResourceHealth/events"
               f"?api-version=2022-10-01&$filter=eventType eq 'ServiceIssue' and status eq 'Active'")
        req = client._client.get(url)  # noqa: SLF001
        resp = client._client._pipeline.run(req).http_response  # noqa: SLF001
        if resp.status_code == 200:
            for ev in resp.json().get("value", []):
                p = ev.get("properties", {})
                rows.append({"name": ev.get("name"), "eventType": p.get("eventType"),
                             "status": p.get("status"), "title": p.get("title"),
                             "impactStartTime": p.get("impactStartTime"),
                             "lastUpdateTime": p.get("lastUpdateTime"),
                             "trackingId": p.get("trackingId")})
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        if exc.status_code in (403, 404):
            return _empty(cols, "Service Health no accesible.")
        raise


def monitor_alerts(cred, sub_id: str) -> pd.DataFrame:
    cols = ["name", "severity", "alertState", "monitorCondition",
            "targetResource", "firedTime"]
    rows: List[dict] = []
    try:
        client = MonitorManagementClient(cred, sub_id)
        for sev in ("Sev0", "Sev1", "Sev2"):
            url = (f"/subscriptions/{sub_id}/providers/Microsoft.AlertsManagement/alerts"
                   f"?api-version=2019-05-05-preview&severity={sev}&monitorCondition=Fired")
            req = client._client.get(url)  # noqa: SLF001
            resp = client._client._pipeline.run(req).http_response  # noqa: SLF001
            if resp.status_code != 200:
                continue
            for al in resp.json().get("value", []):
                ess = al.get("properties", {}).get("essentials", {})
                rows.append({"name": al.get("name"), "severity": ess.get("severity"),
                             "alertState": ess.get("alertState"),
                             "monitorCondition": ess.get("monitorCondition"),
                             "targetResource": ess.get("targetResource"),
                             "firedTime": ess.get("startDateTime")})
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame(rows, columns=cols)


# ════════════════════════════════════════════════════════════════════════════
# 2. COSTOS Y HUÉRFANOS
# ════════════════════════════════════════════════════════════════════════════
def cost_by_service(cred, sub_id: str, days: int = 7) -> pd.DataFrame:
    cols = ["date", "service", "amount", "currency"]
    try:
        client = CostManagementClient(cred)
        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        result = client.query.usage(
            scope=f"/subscriptions/{sub_id}",
            parameters={
                "type": "ActualCost", "timeframe": "Custom",
                "time_period": {
                    "from_property": dt.datetime.combine(start, dt.time.min).isoformat() + "Z",
                    "to": dt.datetime.combine(end, dt.time.max).isoformat() + "Z",
                },
                "dataset": {
                    "granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ServiceName"}],
                },
            },
        )
        col_names = [c.name for c in result.columns]
        rows = []
        for row in result.rows:
            rec = dict(zip(col_names, row))
            rows.append({"date": _fmt_date(rec.get("UsageDate")),
                         "service": rec.get("ServiceName"),
                         "amount": round(float(rec.get("Cost", 0) or 0), 4),
                         "currency": rec.get("Currency")})
        df = pd.DataFrame(rows, columns=cols)
        return df.sort_values(["date", "amount"], ascending=[True, False]).reset_index(drop=True)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        if exc.status_code in (403, 404):
            return _empty(cols, "Cost Management no accesible.")
        raise


def cost_current_month(cred, sub_id: str) -> pd.DataFrame:
    cols = ["date", "service", "amount", "currency"]
    try:
        client = CostManagementClient(cred)
        today = dt.date.today()
        start = today.replace(day=1)
        result = client.query.usage(
            scope=f"/subscriptions/{sub_id}",
            parameters={
                "type": "ActualCost", "timeframe": "Custom",
                "time_period": {
                    "from_property": dt.datetime.combine(start, dt.time.min).isoformat() + "Z",
                    "to": dt.datetime.combine(today, dt.time.max).isoformat() + "Z",
                },
                "dataset": {
                    "granularity": "None",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ServiceName"}],
                },
            },
        )
        col_names = [c.name for c in result.columns]
        rows = [{"date": str(today.replace(day=1)), "service": dict(zip(col_names, r)).get("ServiceName"),
                 "amount": round(float(dict(zip(col_names, r)).get("Cost", 0) or 0), 4),
                 "currency": dict(zip(col_names, r)).get("Currency")}
                for r in result.rows]
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Cost Management (mes actual) no accesible.")


def orphaned_resources(cred, sub_id: str) -> pd.DataFrame:
    cols = ["type", "name", "resourceGroup", "location", "detail", "resourceId"]
    rows: List[dict] = []
    try:
        compute = ComputeManagementClient(cred, sub_id)
        for disk in compute.disks.list():
            if (disk.disk_state or "").lower() == "unattached":
                rows.append({"type": "ManagedDisk (Unattached)", "name": disk.name,
                             "resourceGroup": _rg(disk.id), "location": disk.location,
                             "detail": f"{disk.disk_size_gb} GB / {disk.sku.name if disk.sku else 'n/a'}",
                             "resourceId": disk.id})
        network = NetworkManagementClient(cred, sub_id)
        for pip in network.public_ip_addresses.list_all():
            if pip.ip_configuration is None:
                rows.append({"type": "PublicIP (Unassociated)", "name": pip.name,
                             "resourceGroup": _rg(pip.id), "location": pip.location,
                             "detail": f"{pip.ip_address or 'n/a'} / {pip.sku.name if pip.sku else 'n/a'}",
                             "resourceId": pip.id})
        for lb in network.load_balancers.list_all():
            pools = lb.backend_address_pools or []
            has_members = any(
                getattr(p, "backend_ip_configurations", None) or
                getattr(p, "load_balancer_backend_addresses", None)
                for p in pools
            )
            if not pools or not has_members:
                rows.append({"type": "LoadBalancer (inactivo)", "name": lb.name,
                             "resourceGroup": _rg(lb.id), "location": lb.location,
                             "detail": f"backend_pools={len(pools)}", "resourceId": lb.id})
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Sin permisos para listar recursos.")


def rightsizing(cred, sub_id: str) -> pd.DataFrame:
    cols = ["category", "impact", "problem", "solution", "savings", "resourceId"]
    try:
        client = AdvisorManagementClient(cred, sub_id)
        rows = []
        for rec in client.recommendations.list():
            if str(getattr(rec, "category", "") or "").lower() not in ("cost", "performance"):
                continue
            short = getattr(rec, "short_description", None)
            meta = getattr(rec, "resource_metadata", None)
            ext = getattr(rec, "extended_properties", None) or {}
            rows.append({
                "category": getattr(rec, "category", None),
                "impact": getattr(rec, "impact", None),
                "problem": getattr(short, "problem", "") if short else "",
                "solution": getattr(short, "solution", "") if short else "",
                "savings": ext.get("savingsAmount") or ext.get("annualSavingsAmount"),
                "resourceId": meta.resource_id if meta else getattr(rec, "id", None),
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Azure Advisor no accesible.")


# ── Acciones destructivas (L2) ────────────────────────────────────────────────
def _parse(resource_id: str) -> dict:
    parts = resource_id.split("/")
    lower = [p.lower() for p in parts]
    rg = None
    try:
        rg = parts[lower.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        pass
    return {"rg": rg, "name": parts[-1]}


def delete_disk(cred, sub_id: str, resource_id: str) -> Dict:
    try:
        info = _parse(resource_id)
        ComputeManagementClient(cred, sub_id).disks.begin_delete(info["rg"], info["name"]).result()
        return {"ok": True, "message": f"Disco '{info['name']}' eliminado."}
    except HttpResponseError as exc:
        _guard_http(exc)
        return {"ok": False, "message": str(exc)}


def delete_public_ip(cred, sub_id: str, resource_id: str) -> Dict:
    try:
        info = _parse(resource_id)
        NetworkManagementClient(cred, sub_id).public_ip_addresses.begin_delete(
            info["rg"], info["name"]).result()
        return {"ok": True, "message": f"IP '{info['name']}' liberada."}
    except HttpResponseError as exc:
        _guard_http(exc)
        return {"ok": False, "message": str(exc)}


def delete_lb(cred, sub_id: str, resource_id: str) -> Dict:
    try:
        info = _parse(resource_id)
        NetworkManagementClient(cred, sub_id).load_balancers.begin_delete(
            info["rg"], info["name"]).result()
        return {"ok": True, "message": f"LB '{info['name']}' eliminado."}
    except HttpResponseError as exc:
        _guard_http(exc)
        return {"ok": False, "message": str(exc)}


# ════════════════════════════════════════════════════════════════════════════
# 3. SEGURIDAD
# ════════════════════════════════════════════════════════════════════════════
def security_alerts(cred, sub_id: str) -> pd.DataFrame:
    cols = ["alertDisplayName", "severity", "status", "startTime",
            "description", "alertName"]
    try:
        client = SecurityCenter(cred, sub_id)
        rows = []
        for al in client.alerts.list():
            sev = str(getattr(al, "severity", "") or "")
            if sev.lower() not in ("high", "critical"):
                continue
            rows.append({
                "alertDisplayName": getattr(al, "alert_display_name", None),
                "severity": sev,
                "status": getattr(al, "status", None),
                "startTime": _fmt(getattr(al, "start_time_utc", None)),
                "description": (getattr(al, "description", "") or "")[:200],
                "alertName": getattr(al, "name", None),
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Defender for Cloud no accesible.")


def secure_score(cred, sub_id: str) -> pd.DataFrame:
    cols = ["name", "currentScore", "maxScore", "percentage"]
    try:
        client = SecurityCenter(cred, sub_id)
        rows = []
        for sc in client.secure_scores.list():
            score = getattr(sc, "score", None)
            pct = getattr(score, "percentage", None)
            rows.append({
                "name": getattr(sc, "display_name", None) or getattr(sc, "name", None),
                "currentScore": getattr(score, "current", None),
                "maxScore": getattr(score, "max", None),
                "percentage": round(float(pct) * 100, 2) if pct is not None else None,
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Secure Score no accesible.")


def well_architected_controls(cred, sub_id: str) -> pd.DataFrame:
    cols = ["control", "currentScore", "maxScore", "healthy", "unhealthy"]
    try:
        client = SecurityCenter(cred, sub_id)
        rows = []
        for ctl in client.secure_score_controls.list():
            score = getattr(ctl, "score", None)
            rows.append({
                "control": getattr(ctl, "display_name", None) or getattr(ctl, "name", None),
                "currentScore": getattr(score, "current", None),
                "maxScore": getattr(score, "max", None),
                "healthy": getattr(ctl, "healthy_resource_count", None),
                "unhealthy": getattr(ctl, "unhealthy_resource_count", None),
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Defender for Cloud (controles) no accesible.")


def available_updates(cred, sub_id: str) -> pd.DataFrame:
    cols = ["category", "impact", "problem", "solution", "resourceId"]
    try:
        client = AdvisorManagementClient(cred, sub_id)
        rows = []
        keywords = ("update", "patch", "upgrade", "version", "outdated", "os ")
        for rec in client.recommendations.list():
            short = getattr(rec, "short_description", None)
            problem = getattr(short, "problem", "") if short else ""
            solution = getattr(short, "solution", "") if short else ""
            if any(k in f"{problem} {solution}".lower() for k in keywords):
                meta = getattr(rec, "resource_metadata", None)
                rows.append({"category": getattr(rec, "category", None),
                             "impact": getattr(rec, "impact", None),
                             "problem": problem, "solution": solution,
                             "resourceId": meta.resource_id if meta else getattr(rec, "id", None)})
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Azure Advisor (updates) no accesible.")


def dismiss_security_alert(cred, sub_id: str, alert_name: str,
                           location: str = "centralus") -> Dict:
    try:
        SecurityCenter(cred, sub_id).alerts.update_subscription_level_state_to_dismiss(
            location, alert_name)
        return {"ok": True, "message": f"Alerta '{alert_name}' descartada."}
    except HttpResponseError as exc:
        _guard_http(exc)
        return {"ok": False, "message": str(exc)}


# ════════════════════════════════════════════════════════════════════════════
# 4. CICLO DE VIDA / DEPRECADOS
# ════════════════════════════════════════════════════════════════════════════
def deprecated_services(cred, sub_id: str) -> pd.DataFrame:
    cols = ["category", "impact", "problem", "solution", "resourceId"]
    try:
        client = AdvisorManagementClient(cred, sub_id)
        rows = []
        keywords = ("retire", "retirement", "deprecat", "end of life",
                    "eol", "no longer", "unsupported", "migrate")
        for rec in client.recommendations.list():
            short = getattr(rec, "short_description", None)
            problem = getattr(short, "problem", "") if short else ""
            solution = getattr(short, "solution", "") if short else ""
            if any(k in f"{problem} {solution}".lower() for k in keywords):
                meta = getattr(rec, "resource_metadata", None)
                rows.append({"category": getattr(rec, "category", None),
                             "impact": getattr(rec, "impact", None),
                             "problem": problem, "solution": solution,
                             "resourceId": meta.resource_id if meta else getattr(rec, "id", None)})
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, "Azure Advisor (deprecations) no accesible.")


# ════════════════════════════════════════════════════════════════════════════
# INVENTARIO DE RECURSOS AZURE (módulo 1.2)
# ════════════════════════════════════════════════════════════════════════════

def _power_state_from_vm(vm) -> str:
    """Extrae el PowerState del instance_view ya expandido en la VM (si existe)."""
    iv = getattr(vm, "instance_view", None)
    statuses = getattr(iv, "statuses", None) if iv else None
    if not statuses:
        return "Unknown"
    return next(
        (s.display_status for s in statuses
         if s.code and s.code.startswith("PowerState")),
        "Unknown")


def inventory_vms(cred, sub_id: str) -> pd.DataFrame:
    """
    Inventario de Virtual Machines con estado de ejecucion.

    Usa list_all(status_only="true"): una sola llamada paginada que ya trae
    el instance_view (power state) de todas las VMs de la suscripcion. Evita
    el patron N+1 (un get() por VM) que hacia la consulta lenta.
    """
    cols = ["name", "resourceGroup", "location", "vmSize",
            "osType", "powerState", "provisioningState", "id"]
    try:
        client = ComputeManagementClient(cred, sub_id)
        rows = []
        try:
            vm_iter = client.virtual_machines.list_all(status_only="true")
        except TypeError:
            # Compatibilidad con versiones del SDK que no aceptan status_only
            vm_iter = client.virtual_machines.list_all()
        for vm in vm_iter:
            rows.append({
                "name": vm.name,
                "resourceGroup": _rg(vm.id),
                "location": vm.location,
                "vmSize": (vm.hardware_profile.vm_size
                           if vm.hardware_profile else None),
                "osType": str(getattr(vm.storage_profile.os_disk, "os_type", "")
                              if vm.storage_profile else ""),
                "powerState": _power_state_from_vm(vm),
                "provisioningState": vm.provisioning_state,
                "id": vm.id,
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        if getattr(exc, "status_code", None) in (403, 404):
            return _empty(cols, f"VMs no accesibles (permisos): {exc}")
        raise


def inventory_disks(cred, sub_id: str) -> pd.DataFrame:
    """Inventario de Managed Disks."""
    cols = ["name", "resourceGroup", "location", "diskSizeGB",
            "diskState", "sku", "osType", "timeCreated"]
    try:
        client = ComputeManagementClient(cred, sub_id)
        rows = []
        for disk in client.disks.list():
            rows.append({
                "name": disk.name,
                "resourceGroup": _rg(disk.id),
                "location": disk.location,
                "diskSizeGB": disk.disk_size_gb,
                "diskState": disk.disk_state,
                "sku": disk.sku.name if disk.sku else None,
                "osType": str(disk.os_type) if disk.os_type else None,
                "timeCreated": _fmt(disk.time_created),
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"Discos no accesibles: {exc}")


def inventory_vnets(cred, sub_id: str) -> pd.DataFrame:
    """Inventario de Virtual Networks."""
    cols = ["name", "resourceGroup", "location",
            "addressSpace", "subnets", "provisioningState"]
    try:
        client = NetworkManagementClient(cred, sub_id)
        rows = []
        for vnet in client.virtual_networks.list_all():
            addr = ", ".join(
                vnet.address_space.address_prefixes
                if vnet.address_space else [])
            subs_count = len(vnet.subnets) if vnet.subnets else 0
            rows.append({
                "name": vnet.name,
                "resourceGroup": _rg(vnet.id),
                "location": vnet.location,
                "addressSpace": addr,
                "subnets": subs_count,
                "provisioningState": vnet.provisioning_state,
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"VNets no accesibles: {exc}")


def inventory_storage(cred, sub_id: str) -> pd.DataFrame:
    """Inventario de Storage Accounts."""
    cols = ["name", "resourceGroup", "location", "kind",
            "sku", "accessTier", "primaryEndpointBlob"]
    try:
        from azure.mgmt.storage import StorageManagementClient  # lazy import
        client = StorageManagementClient(cred, sub_id)
        rows = []
        for acc in client.storage_accounts.list():
            rows.append({
                "name": acc.name,
                "resourceGroup": _rg(acc.id),
                "location": acc.location,
                "kind": acc.kind,
                "sku": acc.sku.name if acc.sku else None,
                "accessTier": str(acc.access_tier) if acc.access_tier else None,
                "primaryEndpointBlob": (acc.primary_endpoints.blob
                                        if acc.primary_endpoints else None),
            })
        return pd.DataFrame(rows, columns=cols)
    except ImportError:
        return _empty(cols, "azure-mgmt-storage no instalado.")
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"Storage Accounts no accesibles: {exc}")


def inventory_aks(cred, sub_id: str) -> pd.DataFrame:
    """Inventario de clusters AKS."""
    cols = ["name", "resourceGroup", "location", "kubernetesVersion",
            "provisioningState", "agentPoolCount", "fqdn"]
    try:
        from azure.mgmt.containerservice import ContainerServiceClient  # lazy import
        client = ContainerServiceClient(cred, sub_id)
        rows = []
        for cluster in client.managed_clusters.list():
            agent_count = (len(cluster.agent_pool_profiles)
                           if cluster.agent_pool_profiles else 0)
            rows.append({
                "name": cluster.name,
                "resourceGroup": _rg(cluster.id),
                "location": cluster.location,
                "kubernetesVersion": cluster.kubernetes_version,
                "provisioningState": cluster.provisioning_state,
                "agentPoolCount": agent_count,
                "fqdn": cluster.fqdn,
            })
        return pd.DataFrame(rows, columns=cols)
    except ImportError:
        return _empty(cols, "azure-mgmt-containerservice no instalado.")
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"AKS no accesible: {exc}")


# ── Métricas Azure Monitor para un recurso ────────────────────────────────────
def get_vm_metrics(cred, sub_id: str, resource_id: str,
                   hours: int = 3) -> pd.DataFrame:
    """
    CPU, Network, Disk para una VM de Azure.
    resource_id: ID completo del recurso ARM.
    """
    import datetime as dt
    cols = ["timestamp", "metric", "value", "unit"]
    metrics_to_fetch = [
        ("Percentage CPU",          "Percent",  "Average"),
        ("Network In Total",        "Bytes",    "Total"),
        ("Network Out Total",       "Bytes",    "Total"),
        ("Disk Read Bytes",         "Bytes",    "Total"),
        ("Disk Write Bytes",        "Bytes",    "Total"),
        ("Disk Read Operations/Sec","CountPS",  "Average"),
        ("Disk Write Operations/Sec","CountPS", "Average"),
    ]
    rows = []
    try:
        client = MonitorManagementClient(cred, sub_id)
        end   = dt.datetime.utcnow()
        start = end - dt.timedelta(hours=hours)
        timespan = f"{start.isoformat()}Z/{end.isoformat()}Z"
        for metric_name, unit, agg in metrics_to_fetch:
            try:
                resp = client.metrics.list(
                    resource_uri=resource_id,
                    timespan=timespan,
                    interval="PT5M",
                    metricnames=metric_name,
                    aggregation=agg,
                )
                for metric in resp.value:
                    for ts in (metric.timeseries or []):
                        for dp in (ts.data or []):
                            val = getattr(dp, agg.lower(), None)
                            if val is not None:
                                rows.append({
                                    "timestamp": dp.time_stamp.isoformat(),
                                    "metric": metric_name,
                                    "value": round(float(val), 4),
                                    "unit": unit,
                                })
            except Exception:  # noqa: BLE001
                continue
        return pd.DataFrame(rows, columns=cols)
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"Metricas VM no accesibles: {exc}")


def get_all_alerts_detail(cred, sub_id: str,
                          time_range_hours: int = 24) -> pd.DataFrame:
    """
    Panel de alertas activas de Azure Monitor con filtros Sev 0-2.
    Segun el manual: Last 24h, Sev 0-2, estado Active/New.
    """
    cols = ["name", "severity", "severityLabel", "alertState",
            "monitorCondition", "targetResource", "targetResourceType",
            "signalType", "firedTime", "resolvedTime", "description"]
    rows: List[dict] = []
    sev_labels = {"Sev0": "Critical", "Sev1": "Error", "Sev2": "Warning",
                  "Sev3": "Informational", "Sev4": "Verbose"}
    try:
        client = MonitorManagementClient(cred, sub_id)
        api_version = "2019-05-05-preview"
        # Consultamos Sev 0, 1 y 2 por separado (API no acepta multiselect en severidad)
        for sev in ("Sev0", "Sev1", "Sev2"):
            url = (
                f"/subscriptions/{sub_id}/providers/Microsoft.AlertsManagement/alerts"
                f"?api-version={api_version}&severity={sev}"
                f"&timeRange={time_range_hours}h"
            )
            try:
                req  = client._client.get(url)  # noqa: SLF001
                resp = client._client._pipeline.run(req).http_response  # noqa: SLF001
                if resp.status_code != 200:
                    continue
                for al in resp.json().get("value", []):
                    ess = al.get("properties", {}).get("essentials", {})
                    rows.append({
                        "name": al.get("name"),
                        "severity": ess.get("severity"),
                        "severityLabel": sev_labels.get(ess.get("severity",""), ""),
                        "alertState": ess.get("alertState"),
                        "monitorCondition": ess.get("monitorCondition"),
                        "targetResource": ess.get("targetResource"),
                        "targetResourceType": ess.get("targetResourceType"),
                        "signalType": ess.get("signalType"),
                        "firedTime": ess.get("startDateTime"),
                        "resolvedTime": ess.get("lastModifiedDateTime"),
                        "description": (al.get("properties", {})
                                        .get("context", {})
                                        .get("context", {})
                                        .get("name", "")),
                    })
            except Exception:  # noqa: BLE001
                continue
        df = pd.DataFrame(rows, columns=cols)
        if not df.empty:
            # Ordenar: Critical primero, luego por firedTime desc
            sev_order = {"Sev0": 0, "Sev1": 1, "Sev2": 2}
            df["_sort"] = df["severity"].map(sev_order).fillna(9)
            df = df.sort_values(["_sort", "firedTime"],
                                ascending=[True, False]).drop("_sort", axis=1)
        return df
    except ClientAuthenticationError as exc:
        _raise_auth(exc)
    except HttpResponseError as exc:
        _guard_http(exc)
        return _empty(cols, f"Alertas Azure Monitor no accesibles: {exc}")
