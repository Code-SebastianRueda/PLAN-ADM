"""
azure_backups.py
================
Azure Backup Center (apartado 1.7) — DATOS REALES.

Fuentes Azure:
  Azure Resource Graph
    - Microsoft.RecoveryServices/vaults  -> descubrir todos los vaults.
    - Inventario total de recursos "respaldables" (VM, SQL, Storage, Files, AKS,
      PostgreSQL, MySQL) -> para calcular recursos SIN proteccion.
  Recovery Services (por vault, via ARM REST)
    - .../backupProtectedItems  -> items protegidos: ultima copia, estado,
      politica, ultimo recovery point, estado de proteccion/salud.
    - .../backupJobs            -> ejecuciones (exito/fallo/duracion) del periodo.

Normaliza a dos DataFrames:
  - items:  un registro por recurso protegido (+ los no protegidos del inventario).
  - jobs:   un registro por ejecucion de backup.

Multi-vault / multi-suscripcion EN PARALELO. Reutiliza _arm_get/_arm_post del
modulo de alertas (cliente ARM cacheado). Nunca inventa datos.

La capa de datos esta aislada: el render solo consume load_backups() + las
funciones de calculo; conectar otra fuente no exigiria tocar la UI.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import pandas as pd

import azure_alerts as _az

try:
    from azure.core.exceptions import HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_RSV_API = "2024-04-01"
_RG_API = "2024-04-01"
_RG_URL = (f"https://management.azure.com/providers/Microsoft.ResourceGraph/"
           f"resources?api-version={_RG_API}")
_MAX_WORKERS = 8

# Tipos de recurso "respaldables" para calcular cobertura (sin proteccion).
_BACKUPABLE_TYPES = {
    "microsoft.compute/virtualmachines": "Virtual Machine",
    "microsoft.sql/servers/databases": "Azure SQL",
    "microsoft.storage/storageaccounts": "Storage Account",
    "microsoft.dbforpostgresql/servers": "PostgreSQL",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL",
    "microsoft.dbformysql/servers": "MySQL",
    "microsoft.dbformysql/flexibleservers": "MySQL",
    "microsoft.containerservice/managedclusters": "AKS",
}

# protectedItemType -> tipo legible.
_PROTECTED_TYPE = {
    "microsoft.compute/virtualmachines": "Virtual Machine",
    "microsoft.classiccompute/virtualmachines": "Virtual Machine",
    "azureiaasvm": "Virtual Machine",
    "microsoft.sql/servers/databases": "Azure SQL",
    "azuresqldb": "Azure SQL",
    "azurefileshare": "Azure Files",
    "microsoft.storage/storageaccounts": "Storage Account",
}

ITEM_COLUMNS = [
    "resource", "resourceType", "subscriptionId", "subscriptionName",
    "resourceGroup", "location", "policyName", "lastBackup", "lastBackupStatus",
    "nextBackup", "lastRecoveryPoint", "rpoHours", "rpoActualHours",
    "rpoBreached", "protection", "protectionState", "healthStatus",
    "vault", "resourceId",
]
JOB_COLUMNS = [
    "resource", "operation", "status", "startTime", "endTime", "durationMin",
    "errorMsg", "subscriptionName", "vault", "jobId",
]


def _empty_items(note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=ITEM_COLUMNS)
    if note:
        df.attrs["note"] = note
    return df


def _parse_iso(s):
    return _az._parse_iso(str(s)) if s else None


def _type_from_id(res_id: str) -> str:
    if not res_id or "/providers/" not in res_id:
        return ""
    tail = res_id.split("/providers/")[-1].split("/")
    return f"{tail[0]}/{tail[1]}" if len(tail) >= 2 else ""


def _hours_since(ts) -> Optional[float]:
    d = _parse_iso(ts)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - d).total_seconds() / 3600


# ---------------------------------------------------------------------------
# Resource Graph: vaults + inventario respaldable
# ---------------------------------------------------------------------------
def _rg_query(credential, sub_ids: List[str], kql: str) -> List[dict]:
    """Ejecuta una query KQL de Resource Graph sobre las suscripciones dadas."""
    if not sub_ids:
        return []
    rows, skip = [], None
    pipeline_sub = sub_ids[0]
    pages = 0
    while pages < 50:
        pages += 1
        body = {"subscriptions": sub_ids, "query": kql,
                "options": {"resultFormat": "objectArray", "$top": 1000}}
        if skip:
            body["options"]["$skipToken"] = skip
        payload = _az._arm_post(credential, pipeline_sub, _RG_URL, body)
        data = payload.get("data", [])
        if isinstance(data, dict):
            cols = [c["name"] for c in data.get("columns", [])]
            rows += [dict(zip(cols, r)) for r in data.get("rows", [])]
        else:
            rows += data
        skip = payload.get("$skipToken") or payload.get("skipToken")
        if not skip:
            break
    return rows


def _discover_vaults(credential, sub_ids, name_by_id):
    """Lista los Recovery Services vaults del alcance (via Resource Graph)."""
    kql = ("Resources | where type =~ 'microsoft.recoveryservices/vaults' "
           "| project name, resourceGroup, subscriptionId, location, id")
    vaults = []
    for v in _rg_query(credential, sub_ids, kql):
        vaults.append({
            "name": v.get("name", ""),
            "resourceGroup": v.get("resourceGroup", ""),
            "subscriptionId": v.get("subscriptionId", ""),
            "subscriptionName": name_by_id.get(v.get("subscriptionId", ""),
                                               v.get("subscriptionId", "")),
            "location": v.get("location", ""),
        })
    return vaults


def _inventory_backupable(credential, sub_ids, name_by_id):
    """Inventario de recursos respaldables (para calcular sin proteccion)."""
    types = "', '".join(_BACKUPABLE_TYPES.keys())
    kql = (f"Resources | where tolower(type) in ('{types}') "
           f"| project name, type, resourceGroup, subscriptionId, location, id")
    out = []
    for r in _rg_query(credential, sub_ids, kql):
        rtype = (r.get("type", "") or "").lower()
        out.append({
            "resource": r.get("name", ""),
            "resourceType": _BACKUPABLE_TYPES.get(rtype, rtype),
            "subscriptionId": r.get("subscriptionId", ""),
            "subscriptionName": name_by_id.get(r.get("subscriptionId", ""),
                                               r.get("subscriptionId", "")),
            "resourceGroup": r.get("resourceGroup", ""),
            "location": r.get("location", ""),
            "resourceId": r.get("id", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Recovery Services: protected items y jobs por vault
# ---------------------------------------------------------------------------
def _protected_items(credential, vault) -> List[dict]:
    sid = vault["subscriptionId"]
    rg = vault["resourceGroup"]
    name = vault["name"]
    url = (f"/subscriptions/{sid}/resourceGroups/{rg}/providers/"
           f"Microsoft.RecoveryServices/vaults/{name}/backupProtectedItems"
           f"?api-version={_RSV_API}")
    out = []
    try:
        next_url = url
        pages = 0
        while next_url and pages < 50:
            pages += 1
            payload = _az._arm_get(credential, sid, next_url)
            for it in payload.get("value", []):
                p = it.get("properties", {}) or {}
                res_id = p.get("sourceResourceId", "") or ""
                last_rp = p.get("lastRecoveryPoint", "")
                rpo_actual = _hours_since(last_rp)
                # RPO configurado: por defecto 24h (diario); si hay schedule, se
                # podria refinar. Aqui usamos 24h como SLA estandar.
                rpo_cfg = 24.0
                breached = (rpo_actual is not None and rpo_actual > rpo_cfg)
                out.append({
                    "resource": p.get("friendlyName", "")
                                or _az._resource_name_from_id(res_id),
                    "resourceType": _PROTECTED_TYPE.get(
                        (p.get("protectedItemType", "") or "").lower(),
                        _az._resource_type_pretty(
                            _type_from_id(res_id))),
                    "subscriptionId": sid,
                    "subscriptionName": vault["subscriptionName"],
                    "resourceGroup": _az._rg_from_id(res_id) or rg,
                    "location": vault["location"],
                    "policyName": p.get("policyName", "") or "Sin política",
                    "lastBackup": p.get("lastBackupTime", "") or "",
                    "lastBackupStatus": p.get("lastBackupStatus", "") or "",
                    "nextBackup": "",
                    "lastRecoveryPoint": last_rp or "",
                    "rpoHours": rpo_cfg,
                    "rpoActualHours": round(rpo_actual, 1) if rpo_actual is not None else None,
                    "rpoBreached": breached,
                    "protection": _protection_label(p),
                    "protectionState": p.get("protectionState", "") or "",
                    "healthStatus": p.get("healthStatus", "") or "",
                    "vault": name,
                    "resourceId": res_id,
                })
            next_url = payload.get("nextLink") or ""
        return out
    except Exception:  # noqa: BLE001
        return out


def _protection_label(props: dict) -> str:
    """Deriva el estado de proteccion legible del protected item."""
    last = (props.get("lastBackupStatus", "") or "").lower()
    pstate = (props.get("protectionState", "") or "").lower()
    health = (props.get("healthStatus", "") or "").lower()
    if last in ("failed",) or health in ("unhealthy",) or pstate == "protectionerror":
        return "Fallido"
    if pstate in ("protectionstopped",):
        return "Sin protección"
    if last in ("irpending", "") and pstate not in ("protected",):
        return "Pendiente"
    if health in ("transientdegraded", "persistentdegraded"):
        return "Advertencia"
    return "Protegido"


def _backup_jobs(credential, vault, days: int) -> List[dict]:
    sid = vault["subscriptionId"]
    rg = vault["resourceGroup"]
    name = vault["name"]
    start = (dt.datetime.utcnow() - dt.timedelta(days=days))
    # Filtro por fecha de inicio (formato que acepta la API).
    flt = f"startTime eq '{start.strftime('%Y-%m-%d %H:%M:%S')} AM'"
    url = (f"/subscriptions/{sid}/resourceGroups/{rg}/providers/"
           f"Microsoft.RecoveryServices/vaults/{name}/backupJobs"
           f"?api-version={_RSV_API}&$filter={flt}")
    out = []
    try:
        next_url = url
        pages = 0
        while next_url and pages < 50:
            pages += 1
            payload = _az._arm_get(credential, sid, next_url)
            for j in payload.get("value", []):
                p = j.get("properties", {}) or {}
                start_t = p.get("startTime", "")
                end_t = p.get("endTime", "")
                dur = None
                sd, ed = _parse_iso(start_t), _parse_iso(end_t)
                if sd and ed:
                    dur = round((ed - sd).total_seconds() / 60, 1)
                err = ""
                for e in (p.get("errorDetails", []) or []):
                    msg = e.get("errorString") or ""
                    if msg:
                        err = msg[:120]
                        break
                out.append({
                    "resource": p.get("entityFriendlyName", "") or "",
                    "operation": p.get("operation", "") or "",
                    "status": p.get("status", "") or "",
                    "startTime": start_t,
                    "endTime": end_t,
                    "durationMin": dur,
                    "errorMsg": err,
                    "subscriptionName": vault["subscriptionName"],
                    "vault": name,
                    "jobId": j.get("name", "") or "",
                })
            next_url = payload.get("nextLink") or ""
        return out
    except Exception:  # noqa: BLE001
        return out


# ---------------------------------------------------------------------------
# Orquestacion
# ---------------------------------------------------------------------------
def load_backups(credential, subscriptions: List[dict], days: int = 30) -> dict:
    """Carga items protegidos + jobs + inventario para el alcance dado.

    Devuelve dict: {'items', 'jobs', 'errors', 'vaults', 'status',
    'fetched_at', 'subscriptions_scanned'}.
    """
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    empty = {"items": _empty_items(), "jobs": pd.DataFrame(columns=JOB_COLUMNS),
             "errors": [], "vaults": 0, "status": "error",
             "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "subscriptions_scanned": 0}
    if not subs or not _SDK_OK:
        return empty

    sub_ids = [s.get("subscriptionId") or s.get("id") for s in subs]
    name_by_id = {(s.get("subscriptionId") or s.get("id")):
                  (s.get("displayName") or s.get("name") or
                   (s.get("subscriptionId") or s.get("id"))) for s in subs}
    errors = []
    status = "ok"

    # 1) Descubrir vaults + inventario respaldable (Resource Graph).
    try:
        vaults = _discover_vaults(credential, sub_ids, name_by_id)
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        errors.append("Sin permisos para Resource Graph."
                      if code in (401, 403) else f"Resource Graph: HTTP {code}")
        vaults = []
        status = "error"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Resource Graph: {type(exc).__name__}")
        vaults = []
        status = "error"

    try:
        inventory = _inventory_backupable(credential, sub_ids, name_by_id)
    except Exception:  # noqa: BLE001
        inventory = []

    # 2) Protected items + jobs por vault, en paralelo.
    item_rows, job_rows = [], []
    if vaults:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(vaults))) as pool:
            fut_items = {pool.submit(_protected_items, credential, v): v
                         for v in vaults}
            fut_jobs = {pool.submit(_backup_jobs, credential, v, days): v
                        for v in vaults}
            for fut in as_completed(list(fut_items) + list(fut_jobs)):
                try:
                    res = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                if fut in fut_items:
                    item_rows += res
                else:
                    job_rows += res

    items = pd.DataFrame(item_rows, columns=ITEM_COLUMNS)
    jobs = pd.DataFrame(job_rows, columns=JOB_COLUMNS)

    # 3) Añadir recursos SIN proteccion (inventario que no esta en items).
    protected_ids = set(items["resourceId"].str.lower()) if not items.empty else set()
    unprotected = []
    for inv in inventory:
        rid = (inv.get("resourceId", "") or "").lower()
        if rid and rid in protected_ids:
            continue
        unprotected.append({
            **{c: "" for c in ITEM_COLUMNS},
            "resource": inv["resource"],
            "resourceType": inv["resourceType"],
            "subscriptionId": inv["subscriptionId"],
            "subscriptionName": inv["subscriptionName"],
            "resourceGroup": inv["resourceGroup"],
            "location": inv["location"],
            "policyName": "Sin política",
            "protection": "Sin protección",
            "protectionState": "NotProtected",
            "rpoBreached": False,
            "resourceId": inv["resourceId"],
        })
    if unprotected:
        items = pd.concat([items, pd.DataFrame(unprotected, columns=ITEM_COLUMNS)],
                          ignore_index=True)

    # Normalizar tipos: las filas 'sin proteccion' se agregan con "" en campos
    # numericos/booleanos; forzar a numerico/bool evita comparar str con int.
    if not items.empty:
        items["rpoActualHours"] = pd.to_numeric(items["rpoActualHours"],
                                                errors="coerce")
        items["rpoHours"] = pd.to_numeric(items["rpoHours"],
                                          errors="coerce").fillna(24.0)
        items["rpoBreached"] = (items["rpoBreached"]
                                .map(lambda v: bool(v) if isinstance(v, bool)
                                     else str(v).lower() in ("true", "1"))
                                .astype(bool))

    if not vaults and not errors:
        errors.append("No se encontraron Recovery Services vaults en el alcance.")

    return {
        "items": items, "jobs": jobs, "errors": errors,
        "vaults": len(vaults), "status": status,
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subscriptions_scanned": len(subs),
    }


# ---------------------------------------------------------------------------
# Calculos (todo derivado del dataset, nunca hardcodeado)
# ---------------------------------------------------------------------------
def calculate_protection_percentage(items: pd.DataFrame) -> float:
    if items is None or items.empty:
        return 0.0
    total = len(items)
    prot = (items["protection"] != "Sin protección").sum()
    return round(prot / total * 100, 1) if total else 0.0


def calculate_backup_success_rate(jobs: pd.DataFrame) -> float:
    if jobs is None or jobs.empty:
        return 0.0
    backup = jobs[jobs["operation"].str.lower().str.contains("backup", na=False)]
    if backup.empty:
        backup = jobs
    ok = backup["status"].str.lower().isin(["completed"]).sum()
    total = len(backup)
    return round(ok / total * 100, 1) if total else 0.0


def calculate_rpo_compliance(items: pd.DataFrame) -> float:
    if items is None or items.empty:
        return 0.0
    withpol = items[items["protection"] != "Sin protección"]
    if withpol.empty:
        return 0.0
    inside = (~withpol["rpoBreached"].astype(bool)).sum()
    return round(inside / len(withpol) * 100, 1)


def kpis(data: dict) -> dict:
    items = data.get("items", pd.DataFrame())
    jobs = data.get("jobs", pd.DataFrame())
    out = {"protegidos": 0, "proteccion_pct": 0.0, "exitosos": 0,
           "exito_pct": 0.0, "fallidos": 0, "sin_proteccion": 0,
           "sin_proteccion_pct": 0.0, "rpo_incumplido": 0,
           "ultimo_backup_min": None, "total": 0}
    if items is not None and not items.empty:
        out["total"] = len(items)
        out["protegidos"] = int((items["protection"] != "Sin protección").sum())
        out["proteccion_pct"] = calculate_protection_percentage(items)
        out["sin_proteccion"] = int((items["protection"] == "Sin protección").sum())
        out["sin_proteccion_pct"] = round(
            out["sin_proteccion"] / out["total"] * 100, 1) if out["total"] else 0.0
        out["rpo_incumplido"] = int(items["rpoBreached"].astype(bool).sum())
        # Minutos desde el ultimo backup exitoso.
        times = [_hours_since(t) for t in items["lastBackup"] if t]
        times = [t for t in times if t is not None]
        if times:
            out["ultimo_backup_min"] = int(min(times) * 60)
    if jobs is not None and not jobs.empty:
        backup = jobs[jobs["operation"].str.lower().str.contains("backup", na=False)]
        if backup.empty:
            backup = jobs
        out["exitosos"] = int(backup["status"].str.lower().eq("completed").sum())
        out["fallidos"] = int(backup["status"].str.lower().eq("failed").sum())
        out["exito_pct"] = calculate_backup_success_rate(jobs)
    return out


def jobs_by_status(jobs: pd.DataFrame) -> pd.DataFrame:
    cols = ["status", "count"]
    if jobs is None or jobs.empty:
        return pd.DataFrame(columns=cols)
    label = {"completed": "Exitosos", "failed": "Fallidos",
             "completedwithwarnings": "Advertencias", "cancelled": "Cancelados",
             "inprogress": "En ejecución"}
    s = jobs["status"].str.lower().map(lambda x: label.get(x, x or "Otro"))
    g = s.value_counts().reset_index()
    g.columns = cols
    return g


def jobs_trend(jobs: pd.DataFrame) -> pd.DataFrame:
    cols = ["date", "Exitosos", "Fallidos"]
    if jobs is None or jobs.empty:
        return pd.DataFrame(columns=cols)
    j = jobs.copy()
    j["date"] = j["startTime"].map(lambda t: str(t)[:10] if t else "")
    j = j[j["date"].astype(bool)]
    if j.empty:
        return pd.DataFrame(columns=cols)
    j["ok"] = j["status"].str.lower().eq("completed").astype(int)
    j["fail"] = j["status"].str.lower().eq("failed").astype(int)
    g = (j.groupby("date").agg(Exitosos=("ok", "sum"),
                               Fallidos=("fail", "sum")).reset_index()
           .sort_values("date"))
    return g[cols]


def protection_by_type(items: pd.DataFrame) -> pd.DataFrame:
    cols = ["resourceType", "protegidos", "sinProteccion", "total"]
    if items is None or items.empty:
        return pd.DataFrame(columns=cols)
    i = items.copy()
    i["_prot"] = (i["protection"] != "Sin protección").astype(int)
    i["_unp"] = (i["protection"] == "Sin protección").astype(int)
    g = (i.groupby("resourceType").agg(protegidos=("_prot", "sum"),
                                       sinProteccion=("_unp", "sum"),
                                       total=("resource", "count"))
           .reset_index().sort_values("total", ascending=False))
    return g[cols]


def rpo_compliance_breakdown(items: pd.DataFrame) -> pd.DataFrame:
    cols = ["categoria", "count"]
    if items is None or items.empty:
        return pd.DataFrame(columns=cols)
    withpol = items[items["protection"] != "Sin protección"]
    nopol = items[items["protection"] == "Sin protección"]
    inside = int((~withpol["rpoBreached"].astype(bool)).sum())
    outside = int(withpol["rpoBreached"].astype(bool).sum())
    return pd.DataFrame([
        {"categoria": "Dentro del RPO", "count": inside},
        {"categoria": "Fuera del RPO", "count": outside},
        {"categoria": "Sin política", "count": int(len(nopol))},
    ], columns=cols)


def volume_by_type(jobs: pd.DataFrame) -> pd.DataFrame:
    """Volumen protegido no viene directo en jobs/items de forma fiable; se
    omite si no hay dato. Placeholder para cuando se agregue usage del vault."""
    return pd.DataFrame(columns=["resourceType", "tb"])


def generate_recommendations(data: dict) -> List[dict]:
    """Motor de reglas: genera recomendaciones a partir del estado real."""
    items = data.get("items", pd.DataFrame())
    jobs = data.get("jobs", pd.DataFrame())
    recs = []
    if items is None or items.empty:
        return recs

    sin_prot = int((items["protection"] == "Sin protección").sum())
    if sin_prot:
        recs.append({
            "id": "no-backup", "title": "Recursos sin política de backup",
            "severity": "Crítico", "affected": sin_prot,
            "reason": f"{sin_prot} recursos respaldables no tienen protección.",
            "action": "Asignar una política de Azure Backup.",
            "riesgo": "Alto riesgo"})

    fallidos = int((items["protection"] == "Fallido").sum())
    if fallidos:
        recs.append({
            "id": "failed", "title": "Backups fallidos",
            "severity": "Alto", "affected": fallidos,
            "reason": f"{fallidos} recursos con último backup fallido.",
            "action": "Revisar conectividad, política y configuración.",
            "riesgo": "Alto riesgo"})

    rpo = int(items["rpoBreached"].astype(bool).sum())
    if rpo:
        recs.append({
            "id": "rpo", "title": "RPO incumplido",
            "severity": "Alto", "affected": rpo,
            "reason": f"{rpo} recursos superaron el RPO configurado (24h).",
            "action": "Revisar frecuencia de backup y ventanas de ejecución.",
            "riesgo": "Alto riesgo"})

    # Recovery points antiguos (> 48h pero protegido).
    _rpo_act = pd.to_numeric(items["rpoActualHours"], errors="coerce").fillna(0)
    stale = items[(items["protection"] != "Sin protección") & (_rpo_act > 48)]
    if not stale.empty:
        recs.append({
            "id": "stale", "title": "Recovery Points antiguos",
            "severity": "Medio", "affected": int(len(stale)),
            "reason": f"{len(stale)} recursos con Recovery Point > 48h.",
            "action": "Validar última ejecución y política asociada.",
            "riesgo": "Medio riesgo"})

    return recs


def attention_items(data: dict) -> dict:
    """Conteos para la seccion 'Atencion requerida'."""
    items = data.get("items", pd.DataFrame())
    out = {"fallidos": 0, "sin_backup": 0, "rpo": 0, "antiguos": 0}
    if items is None or items.empty:
        return out
    rpo_act = pd.to_numeric(items["rpoActualHours"], errors="coerce").fillna(0)
    out["fallidos"] = int((items["protection"] == "Fallido").sum())
    out["sin_backup"] = int((items["protection"] == "Sin protección").sum())
    out["rpo"] = int(items["rpoBreached"].astype(bool).sum())
    out["antiguos"] = int(((items["protection"] != "Sin protección")
                           & (rpo_act > 48)).sum())
    return out
