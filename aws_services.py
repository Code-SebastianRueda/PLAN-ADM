"""
aws_services.py
===============
Lógica de conexión y extracción/remediación para AWS, organizada por niveles
operativos del Plan de Administración (PDM):

    - Nivel 1 (L1): Monitoreo (solo lectura).
    - Nivel 2 (L2): Administración/Remediación (acciones destructivas).
    - Nivel 3 (L3): Arquitectura/Gobierno (right-sizing, quotas, WAF).

Autenticación:
    Perfiles de AWS IAM Identity Center (SSO). El usuario ejecuta previamente:

        aws sso login --profile <perfil>

    Este módulo inicializa la sesión de boto3 con ese perfil. Si el token
    expiró, se lanza AWSAuthError para que la UI pida re-login.

Todas las funciones de lectura devuelven pandas.DataFrame. Las de acción (L2)
devuelven un dict {"ok": bool, "message": str}.
"""

from __future__ import annotations

import configparser
import datetime as dt
import os
from typing import Dict, List, Optional

import boto3
import pandas as pd
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)


class AWSAuthError(Exception):
    """Se lanza cuando la sesión SSO no es válida o expiró."""


_AUTH_ERROR_TYPES = (
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
    NoCredentialsError,
    ProfileNotFound,
)

_AUTH_ERROR_CODES = (
    "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
    "UnrecognizedClientException", "AccessDeniedException",
)


# ===========================================================================
# Descubrimiento de perfiles / cuentas
# ===========================================================================
def list_profiles() -> List[str]:
    """Perfiles disponibles en la config local de AWS (~/.aws/config)."""
    try:
        return boto3.Session().available_profiles or []
    except Exception:  # noqa: BLE001
        return []


def describe_profiles() -> pd.DataFrame:
    """
    Lee ~/.aws/config y devuelve detalle de cada perfil SSO (cuenta, rol,
    región) para mostrar al usuario qué cuentas tiene configuradas.
    """
    columns = ["profile", "sso_account_id", "sso_role_name",
               "sso_start_url", "region"]
    config_path = os.path.join(os.path.expanduser("~"), ".aws", "config")
    rows: List[dict] = []
    if not os.path.exists(config_path):
        return pd.DataFrame(rows, columns=columns)

    parser = configparser.ConfigParser()
    try:
        parser.read(config_path)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(rows, columns=columns)

    for section in parser.sections():
        # Las secciones suelen ser "profile <nombre>" salvo default
        name = section.replace("profile ", "").strip()
        data = parser[section]
        rows.append({
            "profile": name,
            "sso_account_id": data.get("sso_account_id"),
            "sso_role_name": data.get("sso_role_name"),
            "sso_start_url": data.get("sso_start_url"),
            "region": data.get("region"),
        })
    return pd.DataFrame(rows, columns=columns)


def sso_login(profile_name: str) -> Dict:
    """
    Dispara el flujo 'aws sso login --profile <perfil>' (abre navegador).
    Devuelve dict con resultado. Requiere AWS CLI v2 instalada.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["aws", "sso", "login", "--profile", profile_name],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            return {"ok": True, "message": f"SSO login correcto para '{profile_name}'."}
        return {"ok": False, "message": proc.stderr.strip() or proc.stdout.strip()}
    except FileNotFoundError:
        return {"ok": False, "message": "AWS CLI no encontrada. Instala AWS CLI v2."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "El login SSO expiró (timeout)."}


# ===========================================================================
# Sesión
# ===========================================================================
def get_session(profile_name: str, region_name: str = "us-east-1") -> boto3.Session:
    """Crea y valida una sesión boto3 usando un perfil SSO."""
    try:
        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        session.client("sts").get_caller_identity()
        return session
    except _AUTH_ERROR_TYPES as exc:
        raise AWSAuthError(
            f"No se pudo autenticar con el perfil SSO '{profile_name}'. "
            f"Ejecuta 'aws sso login --profile {profile_name}'. Detalle: {exc}"
        ) from exc
    except ClientError as exc:
        _raise_if_auth(exc, profile_name)
        raise


def whoami(session: boto3.Session) -> dict:
    return session.client("sts").get_caller_identity()


def _raise_if_auth(exc: ClientError, profile_name: str = "") -> None:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in _AUTH_ERROR_CODES:
        hint = f" Ejecuta 'aws sso login --profile {profile_name}'." if profile_name else \
               " Vuelve a ejecutar 'aws sso login'."
        raise AWSAuthError(
            f"Token SSO expirado o sin permisos ({code}).{hint}"
        ) from exc


def _today() -> dt.date:
    return dt.date.today()


def _date_ago(days: int) -> dt.date:
    return dt.date.today() - dt.timedelta(days=days)


def _days_ago(days: int) -> dt.datetime:
    return dt.datetime.utcnow() - dt.timedelta(days=days)


def _fmt(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _empty_with_note(columns: List[str], note: str) -> pd.DataFrame:
    df = pd.DataFrame(columns=columns)
    df.attrs["note"] = note
    return df


# ===========================================================================
# NIVEL 1 (L1) — Monitoreo (solo lectura)
# ===========================================================================

# ---- 1. Estado de Salud ----------------------------------------------------
def l1_health_events(session: boto3.Session, max_events: int = 100) -> pd.DataFrame:
    columns = ["service", "eventTypeCode", "eventTypeCategory", "region",
               "statusCode", "startTime", "lastUpdatedTime", "arn"]
    try:
        client = session.client("health", region_name="us-east-1")
        paginator = client.get_paginator("describe_events")
        rows = []
        for page in paginator.paginate(
            filter={"startTimes": [{"from": _days_ago(30)}]},
            PaginationConfig={"MaxItems": max_events},
        ):
            for ev in page.get("events", []):
                rows.append({
                    "service": ev.get("service"),
                    "eventTypeCode": ev.get("eventTypeCode"),
                    "eventTypeCategory": ev.get("eventTypeCategory"),
                    "region": ev.get("region"),
                    "statusCode": ev.get("statusCode"),
                    "startTime": _fmt(ev.get("startTime")),
                    "lastUpdatedTime": _fmt(ev.get("lastUpdatedTime")),
                    "arn": ev.get("arn"),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        if exc.response.get("Error", {}).get("Code") == "SubscriptionRequiredException":
            return _empty_with_note(columns,
                "AWS Health API requiere plan Business o Enterprise Support.")
        raise


# ---- 2. Costos --------------------------------------------------------------
def l1_cost_last_7_days(session: boto3.Session) -> pd.DataFrame:
    return _get_cost(session, _date_ago(7), _today(), "DAILY")


def l1_cost_current_month(session: boto3.Session) -> pd.DataFrame:
    first = _today().replace(day=1)
    return _get_cost(session, first, _today() + dt.timedelta(days=1), "MONTHLY")


def _get_cost(session, start: dt.date, end: dt.date, granularity: str) -> pd.DataFrame:
    columns = ["date", "service", "amount", "unit"]
    try:
        client = session.client("ce", region_name="us-east-1")
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity=granularity,
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        rows = []
        for result in resp.get("ResultsByTime", []):
            period = result["TimePeriod"]["Start"]
            for grp in result.get("Groups", []):
                m = grp["Metrics"]["UnblendedCost"]
                rows.append({
                    "date": period,
                    "service": grp["Keys"][0],
                    "amount": round(float(m["Amount"]), 4),
                    "unit": m["Unit"],
                })
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            df = df.sort_values(["date", "amount"], ascending=[True, False])
        return df.reset_index(drop=True)
    except ClientError as exc:
        _raise_if_auth(exc)
        raise


# ---- 3. Recursos Huérfanos --------------------------------------------------
def l1_orphaned_resources(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    """
    Descubrimiento directo (independiente de Trusted Advisor):
      - Volúmenes EBS 'available' (sin adjuntar).
      - Elastic IPs sin asociación.
      - Load Balancers (ALB/NLB) sin targets sanos / sin instancias.
    """
    columns = ["type", "resourceId", "region", "detail", "extra"]
    region = region_name or session.region_name or "us-east-1"
    rows: List[dict] = []
    try:
        ec2 = session.client("ec2", region_name=region)

        # EBS sin adjuntar
        vols = ec2.describe_volumes(
            Filters=[{"Name": "status", "Values": ["available"]}]
        ).get("Volumes", [])
        for v in vols:
            rows.append({
                "type": "EBS Volume (available)",
                "resourceId": v.get("VolumeId"),
                "region": region,
                "detail": f"{v.get('Size')} GiB / {v.get('VolumeType')}",
                "extra": v.get("CreateTime").isoformat() if v.get("CreateTime") else None,
            })

        # Elastic IPs sin asociar
        addresses = ec2.describe_addresses().get("Addresses", [])
        for a in addresses:
            if not a.get("AssociationId") and not a.get("InstanceId"):
                rows.append({
                    "type": "Elastic IP (unassociated)",
                    "resourceId": a.get("AllocationId"),
                    "region": region,
                    "detail": a.get("PublicIp"),
                    "extra": a.get("Domain"),
                })

        # Load Balancers (ELBv2) sin targets registrados/sanos
        try:
            elbv2 = session.client("elbv2", region_name=region)
            lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
            for lb in lbs:
                arn = lb.get("LoadBalancerArn")
                tgs = elbv2.describe_target_groups(
                    LoadBalancerArn=arn
                ).get("TargetGroups", [])
                healthy = 0
                total = 0
                for tg in tgs:
                    health = elbv2.describe_target_health(
                        TargetGroupArn=tg["TargetGroupArn"]
                    ).get("TargetHealthDescriptions", [])
                    total += len(health)
                    healthy += sum(
                        1 for h in health
                        if h.get("TargetHealth", {}).get("State") == "healthy"
                    )
                if total == 0 or healthy == 0:
                    rows.append({
                        "type": "LoadBalancer (inactivo)",
                        "resourceId": lb.get("LoadBalancerName"),
                        "region": region,
                        "detail": f"targets={total}, healthy={healthy}",
                        "extra": arn,
                    })
        except ClientError:
            pass  # elbv2 puede no estar disponible; no rompemos el resto

        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        raise


# ---- 4. Servicios Deprecados / Retiros -------------------------------------
def l1_service_retirements(session: boto3.Session, max_events: int = 100) -> pd.DataFrame:
    columns = ["service", "eventTypeCode", "eventTypeCategory", "region",
               "statusCode", "startTime", "lastUpdatedTime"]
    try:
        client = session.client("health", region_name="us-east-1")
        paginator = client.get_paginator("describe_events")
        rows = []
        for page in paginator.paginate(
            filter={"eventTypeCategories": ["scheduledChange"],
                    "startTimes": [{"from": _days_ago(90)}]},
            PaginationConfig={"MaxItems": max_events},
        ):
            for ev in page.get("events", []):
                code = (ev.get("eventTypeCode") or "").lower()
                if any(k in code for k in ("retire", "deprecat", "end_of", "eol", "shutdown")) \
                        or ev.get("eventTypeCategory") == "scheduledChange":
                    rows.append({
                        "service": ev.get("service"),
                        "eventTypeCode": ev.get("eventTypeCode"),
                        "eventTypeCategory": ev.get("eventTypeCategory"),
                        "region": ev.get("region"),
                        "statusCode": ev.get("statusCode"),
                        "startTime": _fmt(ev.get("startTime")),
                        "lastUpdatedTime": _fmt(ev.get("lastUpdatedTime")),
                    })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        if exc.response.get("Error", {}).get("Code") == "SubscriptionRequiredException":
            return _empty_with_note(columns,
                "AWS Health API requiere plan Business o Enterprise Support.")
        raise


# ---- 5. Seguridad y Alertas -------------------------------------------------
def l1_security_findings(session: boto3.Session, max_findings: int = 100) -> pd.DataFrame:
    columns = ["findingId", "title", "severity", "productName", "resourceType",
               "resourceId", "complianceStatus", "updatedAt"]
    try:
        client = session.client("securityhub")
        paginator = client.get_paginator("get_findings")
        rows = []
        filters = {
            "SeverityLabel": [
                {"Value": "CRITICAL", "Comparison": "EQUALS"},
                {"Value": "HIGH", "Comparison": "EQUALS"},
            ],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
            "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
        }
        for page in paginator.paginate(Filters=filters,
                                       PaginationConfig={"MaxItems": max_findings}):
            for f in page.get("Findings", []):
                res = (f.get("Resources") or [{}])[0]
                rows.append({
                    "findingId": f.get("Id"),
                    "title": f.get("Title"),
                    "severity": f.get("Severity", {}).get("Label"),
                    "productName": f.get("ProductName"),
                    "resourceType": res.get("Type"),
                    "resourceId": res.get("Id"),
                    "complianceStatus": f.get("Compliance", {}).get("Status"),
                    "updatedAt": f.get("UpdatedAt"),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        if exc.response.get("Error", {}).get("Code") in ("InvalidAccessException", "AccessDeniedException"):
            return _empty_with_note(columns,
                "Security Hub no está habilitado o falta permiso en esta cuenta/región.")
        raise


def l1_cloudwatch_alarms(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    columns = ["alarmName", "stateValue", "metricName", "namespace",
               "stateReason", "stateUpdatedTimestamp", "region"]
    try:
        region = region_name or session.region_name or "us-east-1"
        client = session.client("cloudwatch", region_name=region)
        paginator = client.get_paginator("describe_alarms")
        rows = []
        for page in paginator.paginate(StateValue="ALARM"):
            for al in page.get("MetricAlarms", []):
                rows.append({
                    "alarmName": al.get("AlarmName"),
                    "stateValue": al.get("StateValue"),
                    "metricName": al.get("MetricName"),
                    "namespace": al.get("Namespace"),
                    "stateReason": al.get("StateReason"),
                    "stateUpdatedTimestamp": _fmt(al.get("StateUpdatedTimestamp")),
                    "region": region,
                })
            for al in page.get("CompositeAlarms", []):
                rows.append({
                    "alarmName": al.get("AlarmName"),
                    "stateValue": al.get("StateValue"),
                    "metricName": "(composite)",
                    "namespace": "(composite)",
                    "stateReason": al.get("StateReason"),
                    "stateUpdatedTimestamp": _fmt(al.get("StateUpdatedTimestamp")),
                    "region": region,
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        raise


# ===========================================================================
# NIVEL 2 (L2) — Administración / Remediación (ACCIONES DESTRUCTIVAS)
# ===========================================================================
def l2_delete_ebs_volume(session: boto3.Session, volume_id: str,
                         region_name: str) -> Dict:
    """Elimina un volumen EBS (debe estar 'available')."""
    try:
        ec2 = session.client("ec2", region_name=region_name)
        ec2.delete_volume(VolumeId=volume_id)
        return {"ok": True, "message": f"Volumen {volume_id} eliminado."}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo eliminar {volume_id}: {exc}"}


def l2_release_elastic_ip(session: boto3.Session, allocation_id: str,
                          region_name: str) -> Dict:
    """Libera una Elastic IP no asociada."""
    try:
        ec2 = session.client("ec2", region_name=region_name)
        ec2.release_address(AllocationId=allocation_id)
        return {"ok": True, "message": f"Elastic IP {allocation_id} liberada."}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo liberar {allocation_id}: {exc}"}


def l2_delete_load_balancer(session: boto3.Session, lb_arn: str,
                            region_name: str) -> Dict:
    """Elimina un Load Balancer (ELBv2) inactivo."""
    try:
        elbv2 = session.client("elbv2", region_name=region_name)
        elbv2.delete_load_balancer(LoadBalancerArn=lb_arn)
        return {"ok": True, "message": "Load Balancer eliminado."}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo eliminar el LB: {exc}"}


def l2_set_finding_workflow(session: boto3.Session, finding_id: str,
                            product_arn: str, status: str = "RESOLVED") -> Dict:
    """
    Remediación de seguridad: marca un hallazgo de Security Hub como
    RESOLVED / NOTIFIED / SUPPRESSED (workflow status).
    """
    try:
        client = session.client("securityhub")
        client.batch_update_findings(
            FindingIdentifiers=[{"Id": finding_id, "ProductArn": product_arn}],
            Workflow={"Status": status},
        )
        return {"ok": True, "message": f"Hallazgo marcado como {status}."}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo actualizar el hallazgo: {exc}"}


# ===========================================================================
# NIVEL 3 (L3) — Arquitectura / Gobierno / Optimización
# ===========================================================================
def l3_compute_optimizer(session: boto3.Session) -> pd.DataFrame:
    """Recomendaciones de right-sizing de AWS Compute Optimizer (EC2)."""
    columns = ["instanceArn", "instanceName", "currentType", "finding",
               "recommendedType", "estimatedMonthlySavings"]
    try:
        client = session.client("compute-optimizer", region_name=session.region_name or "us-east-1")
        rows = []
        paginator = client.get_paginator("get_ec2_instance_recommendations")
        for page in paginator.paginate():
            for rec in page.get("instanceRecommendations", []):
                options = rec.get("recommendationOptions", [])
                best = options[0] if options else {}
                savings = None
                for opt in options:
                    s = opt.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {})
                    if s.get("value"):
                        savings = f"{s.get('value')} {s.get('currency', 'USD')}"
                        break
                rows.append({
                    "instanceArn": rec.get("instanceArn"),
                    "instanceName": rec.get("instanceName"),
                    "currentType": rec.get("currentInstanceType"),
                    "finding": rec.get("finding"),
                    "recommendedType": best.get("instanceType"),
                    "estimatedMonthlySavings": savings,
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("OptInRequiredException", "AccessDeniedException"):
            return _empty_with_note(columns,
                "Compute Optimizer no está habilitado (opt-in) en esta cuenta.")
        raise


def l3_cost_by_service_month(session: boto3.Session) -> pd.DataFrame:
    """Gobierno de costos: gasto mensual por servicio (para dashboards CUDOS)."""
    return l1_cost_current_month(session)


def l3_cudos_dashboard_check(session: boto3.Session) -> pd.DataFrame:
    """
    Validación de despliegue de dashboards tipo CUDOS: verifica si existe la
    base de datos de CUR/Athena y datasets de QuickSight típicos del framework.
    Es una validación best-effort (no destructiva).
    """
    columns = ["check", "status", "detail"]
    rows: List[dict] = []
    region = session.region_name or "us-east-1"

    # Athena: buscar base de datos 'cudos'/'cur'
    try:
        athena = session.client("athena", region_name=region)
        dbs = []
        catalogs = athena.list_data_catalogs().get("DataCatalogsSummary", [])
        for cat in catalogs:
            try:
                resp = athena.list_databases(CatalogName=cat["CatalogName"])
                dbs += [d["Name"] for d in resp.get("DatabaseList", [])]
            except ClientError:
                continue
        cudos_dbs = [d for d in dbs if any(k in d.lower() for k in ("cudos", "cur", "cost"))]
        rows.append({
            "check": "Athena CUR/CUDOS database",
            "status": "OK" if cudos_dbs else "NO ENCONTRADO",
            "detail": ", ".join(cudos_dbs) if cudos_dbs else "Sin bases CUR/CUDOS detectadas",
        })
    except ClientError as exc:
        rows.append({"check": "Athena CUR/CUDOS database", "status": "N/D",
                     "detail": str(exc)})

    # QuickSight: datasets
    try:
        acct = whoami(session).get("Account")
        qs = session.client("quicksight", region_name=region)
        datasets = qs.list_data_sets(AwsAccountId=acct).get("DataSetSummaries", [])
        cudos_ds = [d["Name"] for d in datasets
                    if any(k in (d.get("Name", "").lower()) for k in ("cudos", "cost", "cur"))]
        rows.append({
            "check": "QuickSight CUDOS datasets",
            "status": "OK" if cudos_ds else "NO ENCONTRADO",
            "detail": ", ".join(cudos_ds) if cudos_ds else "Sin datasets CUDOS detectados",
        })
    except ClientError as exc:
        rows.append({"check": "QuickSight CUDOS datasets", "status": "N/D",
                     "detail": str(exc)})

    return pd.DataFrame(rows, columns=columns)


def l3_service_quotas(session: boto3.Session, threshold_pct: float = 80.0) -> pd.DataFrame:
    """
    Cuotas de servicio próximas a superarse. Para servicios clave, compara el
    uso actual (vía CloudWatch usage metrics cuando existan) contra el límite.
    Best-effort: lista cuotas con valor definido de servicios principales.
    """
    columns = ["serviceCode", "quotaName", "value", "unit", "adjustable"]
    rows: List[dict] = []
    region = session.region_name or "us-east-1"
    try:
        sq = session.client("service-quotas", region_name=region)
        key_services = ["ec2", "vpc", "lambda", "rds", "elasticloadbalancing", "s3"]
        for svc in key_services:
            try:
                paginator = sq.get_paginator("list_service_quotas")
                for page in paginator.paginate(ServiceCode=svc):
                    for q in page.get("Quotas", []):
                        rows.append({
                            "serviceCode": svc,
                            "quotaName": q.get("QuotaName"),
                            "value": q.get("Value"),
                            "unit": q.get("Unit"),
                            "adjustable": q.get("Adjustable"),
                        })
            except ClientError:
                continue
        df = pd.DataFrame(rows, columns=columns)
        return df
    except ClientError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"Service Quotas no accesible: {exc}")


def l3_well_architected_security(session: boto3.Session) -> pd.DataFrame:
    """
    Postura avanzada de seguridad/arquitectura: resumen de estándares de
    Security Hub habilitados y su nivel de cumplimiento.
    """
    columns = ["standard", "status", "enabledAt"]
    try:
        client = session.client("securityhub")
        subs = client.get_enabled_standards().get("StandardsSubscriptions", [])
        rows = []
        for s in subs:
            rows.append({
                "standard": s.get("StandardsArn", "").split("/")[-2:],
                "status": s.get("StandardsStatus"),
                "enabledAt": _fmt(s.get("UpdatedAt")),
            })
        # Aplanamos el nombre del estándar
        for r in rows:
            if isinstance(r["standard"], list):
                r["standard"] = "/".join(r["standard"])
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        if exc.response.get("Error", {}).get("Code") in ("InvalidAccessException", "AccessDeniedException"):
            return _empty_with_note(columns, "Security Hub no habilitado en esta cuenta/región.")
        raise


# ===========================================================================
# EXTENSIONES PDM — Backups, Parches, Mantenimiento, Savings Plans
# ===========================================================================

# ---- L1 · 1.7 Respaldo de Servicios (AWS Backup) ---------------------------
def l1_backup_status(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    """Estado de los backups recientes de AWS Backup (últimos jobs)."""
    columns = ["backupJobId", "resourceType", "resourceArn", "state",
               "creationDate", "backupVaultName", "percentDone"]
    region = region_name or session.region_name or "us-east-1"
    try:
        client = session.client("backup", region_name=region)
        rows = []
        resp = client.list_backup_jobs(MaxResults=100)
        for job in resp.get("BackupJobs", []):
            rows.append({
                "backupJobId": job.get("BackupJobId"),
                "resourceType": job.get("ResourceType"),
                "resourceArn": job.get("ResourceArn"),
                "state": job.get("State"),
                "creationDate": _fmt(job.get("CreationDate")),
                "backupVaultName": job.get("BackupVaultName"),
                "percentDone": job.get("PercentDone"),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        if exc.response.get("Error", {}).get("Code") in ("AccessDeniedException",):
            return _empty_with_note(columns, "Sin permisos para AWS Backup en esta región.")
        return _empty_with_note(columns, f"AWS Backup no disponible: {exc}")


# ---- L2 · 1.2 Upgrades y Parches (SSM Patch Manager) -----------------------
def l2_available_patches(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    """
    Estado de parches por instancia gestionada por SSM (instancias con parches
    faltantes / no compliant).
    """
    columns = ["instanceId", "patchGroup", "installedCount", "missingCount",
               "failedCount", "complianceStatus", "operationEndTime"]
    region = region_name or session.region_name or "us-east-1"
    try:
        ssm = session.client("ssm", region_name=region)
        rows = []
        paginator = ssm.get_paginator("describe_instance_patch_states")
        for page in paginator.paginate():
            for st_ in page.get("InstancePatchStates", []):
                missing = st_.get("MissingCount", 0)
                failed = st_.get("FailedCount", 0)
                status = "NON_COMPLIANT" if (missing or failed) else "COMPLIANT"
                rows.append({
                    "instanceId": st_.get("InstanceId"),
                    "patchGroup": st_.get("PatchGroup"),
                    "installedCount": st_.get("InstalledCount", 0),
                    "missingCount": missing,
                    "failedCount": failed,
                    "complianceStatus": status,
                    "operationEndTime": _fmt(st_.get("OperationEndTime")),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"SSM Patch Manager no disponible: {exc}")


def l2_run_patch_baseline(session: boto3.Session, instance_ids: List[str],
                          region_name: str, operation: str = "Install") -> Dict:
    """Ejecuta AWS-RunPatchBaseline vía SSM sobre instancias (acción de cambio)."""
    try:
        ssm = session.client("ssm", region_name=region_name)
        resp = ssm.send_command(
            InstanceIds=instance_ids,
            DocumentName="AWS-RunPatchBaseline",
            Parameters={"Operation": [operation]},
        )
        cmd_id = resp.get("Command", {}).get("CommandId")
        return {"ok": True, "message": f"Comando de parcheo enviado (CommandId={cmd_id})."}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo enviar el parcheo: {exc}"}


# ---- L2 · 1.5 Ventanas de Mantenimiento (SSM Maintenance Windows) ----------
def l2_maintenance_windows(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    """Ventanas de mantenimiento configuradas en SSM."""
    columns = ["windowId", "name", "enabled", "schedule", "duration",
               "nextExecutionTime"]
    region = region_name or session.region_name or "us-east-1"
    try:
        ssm = session.client("ssm", region_name=region)
        rows = []
        paginator = ssm.get_paginator("describe_maintenance_windows")
        for page in paginator.paginate():
            for w in page.get("WindowIdentities", []):
                rows.append({
                    "windowId": w.get("WindowId"),
                    "name": w.get("Name"),
                    "enabled": w.get("Enabled"),
                    "schedule": w.get("Schedule"),
                    "duration": w.get("Duration"),
                    "nextExecutionTime": w.get("NextExecutionTime"),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"SSM Maintenance Windows no disponible: {exc}")


def l2_create_maintenance_window(session: boto3.Session, name: str, schedule: str,
                                 duration: int, cutoff: int, region_name: str) -> Dict:
    """Crea una ventana de mantenimiento (planificador de cambios)."""
    try:
        ssm = session.client("ssm", region_name=region_name)
        resp = ssm.create_maintenance_window(
            Name=name, Schedule=schedule, Duration=duration,
            Cutoff=cutoff, AllowUnassociatedTargets=True,
        )
        return {"ok": True, "message": f"Ventana creada: {resp.get('WindowId')}"}
    except ClientError as exc:
        _raise_if_auth(exc)
        return {"ok": False, "message": f"No se pudo crear la ventana: {exc}"}


# ---- L3 · Proyecciones financieras (Savings Plans / Reservas) --------------
def l3_savings_plans_recommendations(session: boto3.Session) -> pd.DataFrame:
    """Recomendaciones de Savings Plans de Cost Explorer."""
    columns = ["accountId", "term", "paymentOption", "hourlyCommitment",
               "estimatedMonthlySavings", "estimatedSavingsPct"]
    try:
        ce = session.client("ce", region_name="us-east-1")
        resp = ce.get_savings_plans_purchase_recommendation(
            SavingsPlansType="COMPUTE_SP",
            TermInYears="ONE_YEAR",
            PaymentOption="NO_UPFRONT",
            LookbackPeriodInDays="THIRTY_DAYS",
        )
        detail = resp.get("SavingsPlansPurchaseRecommendation", {})
        rows = []
        for d in detail.get("SavingsPlansPurchaseRecommendationDetails", []):
            rows.append({
                "accountId": d.get("AccountId"),
                "term": "1yr",
                "paymentOption": "NO_UPFRONT",
                "hourlyCommitment": d.get("HourlyCommitmentToPurchase"),
                "estimatedMonthlySavings": d.get("EstimatedMonthlySavingsAmount"),
                "estimatedSavingsPct": d.get("EstimatedSavingsPercentage"),
            })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"Savings Plans recommendations no disponible: {exc}")


# ---- L3 · Playbooks de automatización (SSM Documents) ----------------------
def l3_automation_playbooks(session: boto3.Session, region_name: Optional[str] = None) -> pd.DataFrame:
    """Documentos de automatización (playbooks) de SSM propiedad de la cuenta."""
    columns = ["name", "documentType", "documentFormat", "owner", "platformTypes"]
    region = region_name or session.region_name or "us-east-1"
    try:
        ssm = session.client("ssm", region_name=region)
        rows = []
        paginator = ssm.get_paginator("list_documents")
        for page in paginator.paginate(
            Filters=[{"Key": "Owner", "Values": ["Self"]}]
        ):
            for d in page.get("DocumentIdentifiers", []):
                rows.append({
                    "name": d.get("Name"),
                    "documentType": d.get("DocumentType"),
                    "documentFormat": d.get("DocumentFormat"),
                    "owner": d.get("Owner"),
                    "platformTypes": ", ".join(d.get("PlatformTypes", []) or []),
                })
        return pd.DataFrame(rows, columns=columns)
    except ClientError as exc:
        _raise_if_auth(exc)
        return _empty_with_note(columns, f"SSM Documents no disponible: {exc}")
