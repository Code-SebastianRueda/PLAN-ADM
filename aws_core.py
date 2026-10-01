"""
aws_core.py
===========
Lógica de conexión, extracción y acciones para AWS.

Autenticación:
    Perfiles AWS IAM Identity Center (SSO). El usuario ejecuta previamente:
        aws sso login --profile <perfil>
    Si el token expiró se lanza AWSAuthError para que la UI solicite re-login.

Lecturas   -> pandas.DataFrame
Acciones   -> dict {"ok": bool, "message": str}
"""

from __future__ import annotations

import configparser
import datetime as dt
import os
import subprocess
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


# ── Excepción de autenticación ──────────────────────────────────────────────
class AWSAuthError(Exception):
    pass


_AUTH_EXCS = (TokenRetrievalError, UnauthorizedSSOTokenError,
              NoCredentialsError, ProfileNotFound)
_AUTH_CODES = ("ExpiredToken", "ExpiredTokenException",
               "InvalidClientTokenId", "UnrecognizedClientException")


def _guard(exc: ClientError, profile: str = "") -> None:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in _AUTH_CODES:
        hint = f" Ejecuta 'aws sso login --profile {profile}'." if profile else ""
        raise AWSAuthError(f"Token SSO expirado ({code}).{hint}") from exc


def _fmt(v):
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v


def _empty(cols: List[str], note: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=cols)
    if note:
        df.attrs["note"] = note
    return df


# ── Descubrimiento de perfiles ───────────────────────────────────────────────
def list_profiles() -> List[str]:
    try:
        return boto3.Session().available_profiles or []
    except Exception:
        return []


def describe_profiles() -> pd.DataFrame:
    cols = ["profile", "sso_account_id", "sso_role_name", "sso_start_url", "region"]
    path = os.path.join(os.path.expanduser("~"), ".aws", "config")
    rows: List[dict] = []
    if not os.path.exists(path):
        return pd.DataFrame(rows, columns=cols)
    parser = configparser.ConfigParser()
    parser.read(path)
    for section in parser.sections():
        name = section.replace("profile ", "").strip()
        d = parser[section]
        rows.append({
            "profile": name,
            "sso_account_id": d.get("sso_account_id"),
            "sso_role_name": d.get("sso_role_name"),
            "sso_start_url": d.get("sso_start_url"),
            "region": d.get("region"),
        })
    return pd.DataFrame(rows, columns=cols)


def sso_login(profile: str) -> Dict:
    try:
        proc = subprocess.run(
            ["aws", "sso", "login", "--profile", profile],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            return {"ok": True, "message": f"SSO login correcto para '{profile}'."}
        return {"ok": False, "message": proc.stderr.strip() or proc.stdout.strip()}
    except FileNotFoundError:
        return {"ok": False, "message": "AWS CLI no encontrada. Instala AWS CLI v2."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Timeout en aws sso login."}


# ── Sesión ───────────────────────────────────────────────────────────────────
def get_session(profile: str, region: str = "us-east-1") -> boto3.Session:
    try:
        s = boto3.Session(profile_name=profile, region_name=region)
        s.client("sts").get_caller_identity()
        return s
    except _AUTH_EXCS as exc:
        raise AWSAuthError(
            f"Perfil SSO '{profile}' no válido. "
            f"Ejecuta 'aws sso login --profile {profile}'. Detalle: {exc}"
        ) from exc
    except ClientError as exc:
        _guard(exc, profile)
        raise


def whoami(s: boto3.Session) -> dict:
    return s.client("sts").get_caller_identity()


# ════════════════════════════════════════════════════════════════════════════
# 1. SALUD DE INFRAESTRUCTURA
# ════════════════════════════════════════════════════════════════════════════
def health_events(s: boto3.Session, days: int = 30) -> pd.DataFrame:
    """Eventos de AWS Health (últimos N días)."""
    cols = ["service", "eventTypeCode", "eventTypeCategory",
            "region", "statusCode", "startTime", "lastUpdatedTime", "arn"]
    try:
        client = s.client("health", region_name="us-east-1")
        pag = client.get_paginator("describe_events")
        rows = []
        for page in pag.paginate(
            filter={"startTimes": [{"from": dt.datetime.utcnow() - dt.timedelta(days=days)}]},
            PaginationConfig={"MaxItems": 200},
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
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        if exc.response["Error"]["Code"] == "SubscriptionRequiredException":
            return _empty(cols, "AWS Health requiere plan Business/Enterprise Support.")
        raise


# ════════════════════════════════════════════════════════════════════════════
# 2. COSTOS Y RECURSOS HUÉRFANOS
# ════════════════════════════════════════════════════════════════════════════
def cost_by_service(s: boto3.Session, days: int = 7) -> pd.DataFrame:
    """Costo diario por servicio (últimos N días, Cost Explorer)."""
    cols = ["date", "service", "amount", "unit"]
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        resp = s.client("ce", region_name="us-east-1").get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
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
        df = pd.DataFrame(rows, columns=cols)
        return df.sort_values(["date", "amount"], ascending=[True, False]).reset_index(drop=True)
    except ClientError as exc:
        _guard(exc)
        raise


def cost_current_month(s: boto3.Session) -> pd.DataFrame:
    """Costo del mes actual por servicio (Cost Explorer)."""
    cols = ["date", "service", "amount", "unit"]
    try:
        today = dt.date.today()
        start = today.replace(day=1)
        end = today + dt.timedelta(days=1)
        resp = s.client("ce", region_name="us-east-1").get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
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
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        raise


def orphaned_resources(s: boto3.Session, region: str) -> pd.DataFrame:
    """EBS 'available', Elastic IPs sueltas, LB sin targets sanos."""
    cols = ["type", "resourceId", "region", "detail", "extra"]
    rows: List[dict] = []
    try:
        ec2 = s.client("ec2", region_name=region)
        for v in ec2.describe_volumes(
            Filters=[{"Name": "status", "Values": ["available"]}]
        ).get("Volumes", []):
            rows.append({"type": "EBS (available)", "resourceId": v["VolumeId"],
                         "region": region, "detail": f"{v.get('Size')} GiB / {v.get('VolumeType')}",
                         "extra": _fmt(v.get("CreateTime"))})
        for a in ec2.describe_addresses().get("Addresses", []):
            if not a.get("AssociationId") and not a.get("InstanceId"):
                rows.append({"type": "Elastic IP (free)", "resourceId": a.get("AllocationId"),
                             "region": region, "detail": a.get("PublicIp"), "extra": a.get("Domain")})
        try:
            elbv2 = s.client("elbv2", region_name=region)
            for lb in elbv2.describe_load_balancers().get("LoadBalancers", []):
                arn = lb["LoadBalancerArn"]
                total = healthy = 0
                for tg in elbv2.describe_target_groups(LoadBalancerArn=arn).get("TargetGroups", []):
                    for h in elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"]).get("TargetHealthDescriptions", []):
                        total += 1
                        if h.get("TargetHealth", {}).get("State") == "healthy":
                            healthy += 1
                if total == 0 or healthy == 0:
                    rows.append({"type": "LB (inactivo)", "resourceId": lb.get("LoadBalancerName"),
                                 "region": region, "detail": f"targets={total} healthy={healthy}",
                                 "extra": arn})
        except ClientError:
            pass
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        raise


def rightsizing(s: boto3.Session) -> pd.DataFrame:
    """Recomendaciones de AWS Compute Optimizer (right-sizing EC2)."""
    cols = ["instanceArn", "instanceName", "currentType", "finding",
            "recommendedType", "estimatedMonthlySavings"]
    try:
        pag = s.client("compute-optimizer",
                       region_name=s.region_name or "us-east-1").get_paginator(
            "get_ec2_instance_recommendations")
        rows = []
        for page in pag.paginate():
            for rec in page.get("instanceRecommendations", []):
                opts = rec.get("recommendationOptions", [])
                best = opts[0] if opts else {}
                sav = None
                for o in opts:
                    sv = o.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {})
                    if sv.get("value"):
                        sav = f"{sv['value']} {sv.get('currency','USD')}"
                        break
                rows.append({
                    "instanceArn": rec.get("instanceArn"),
                    "instanceName": rec.get("instanceName"),
                    "currentType": rec.get("currentInstanceType"),
                    "finding": rec.get("finding"),
                    "recommendedType": best.get("instanceType"),
                    "estimatedMonthlySavings": sav,
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        code = exc.response["Error"]["Code"]
        if code in ("OptInRequiredException", "AccessDeniedException"):
            return _empty(cols, "Compute Optimizer no habilitado (opt-in requerido).")
        raise


def savings_plans(s: boto3.Session) -> pd.DataFrame:
    """Recomendaciones de Savings Plans (Compute SP, 1 año, sin anticipo)."""
    cols = ["accountId", "hourlyCommitment", "estimatedMonthlySavings", "estimatedSavingsPct"]
    try:
        resp = s.client("ce", region_name="us-east-1").get_savings_plans_purchase_recommendation(
            SavingsPlansType="COMPUTE_SP", TermInYears="ONE_YEAR",
            PaymentOption="NO_UPFRONT", LookbackPeriodInDays="THIRTY_DAYS",
        )
        rows = []
        for d in resp.get("SavingsPlansPurchaseRecommendation", {}).get(
                "SavingsPlansPurchaseRecommendationDetails", []):
            rows.append({
                "accountId": d.get("AccountId"),
                "hourlyCommitment": d.get("HourlyCommitmentToPurchase"),
                "estimatedMonthlySavings": d.get("EstimatedMonthlySavingsAmount"),
                "estimatedSavingsPct": d.get("EstimatedSavingsPercentage"),
            })
        return pd.DataFrame(rows, columns=cols) if rows else _empty(cols, "Sin recomendaciones de Savings Plans.")
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"Savings Plans no disponible: {exc}")


# ── Acciones destructivas (L2) ───────────────────────────────────────────────
def delete_ebs_volume(s: boto3.Session, volume_id: str, region: str) -> Dict:
    try:
        s.client("ec2", region_name=region).delete_volume(VolumeId=volume_id)
        return {"ok": True, "message": f"Volumen {volume_id} eliminado."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


def release_eip(s: boto3.Session, allocation_id: str, region: str) -> Dict:
    try:
        s.client("ec2", region_name=region).release_address(AllocationId=allocation_id)
        return {"ok": True, "message": f"EIP {allocation_id} liberada."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


def delete_lb(s: boto3.Session, lb_arn: str, region: str) -> Dict:
    try:
        s.client("elbv2", region_name=region).delete_load_balancer(LoadBalancerArn=lb_arn)
        return {"ok": True, "message": "Load Balancer eliminado."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


def stop_instance(s: boto3.Session, instance_id: str, region: str) -> Dict:
    try:
        s.client("ec2", region_name=region).stop_instances(InstanceIds=[instance_id])
        return {"ok": True, "message": f"Instancia {instance_id} detenida."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


# ════════════════════════════════════════════════════════════════════════════
# 3. SEGURIDAD Y CUMPLIMIENTO
# ════════════════════════════════════════════════════════════════════════════
def security_findings(s: boto3.Session, max_items: int = 100) -> pd.DataFrame:
    """Security Hub: hallazgos CRITICAL/HIGH activos."""
    cols = ["findingId", "title", "severity", "productName",
            "resourceType", "resourceId", "complianceStatus", "updatedAt"]
    try:
        pag = s.client("securityhub").get_paginator("get_findings")
        rows = []
        for page in pag.paginate(
            Filters={
                "SeverityLabel": [{"Value": "CRITICAL", "Comparison": "EQUALS"},
                                  {"Value": "HIGH", "Comparison": "EQUALS"}],
                "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
            },
            PaginationConfig={"MaxItems": max_items},
        ):
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
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        if exc.response["Error"]["Code"] in ("InvalidAccessException", "AccessDeniedException"):
            return _empty(cols, "Security Hub no habilitado o sin permisos.")
        raise


def patch_compliance(s: boto3.Session, region: str) -> pd.DataFrame:
    """Estado de parches SSM (instancias non-compliant)."""
    cols = ["instanceId", "patchGroup", "missingCount",
            "failedCount", "complianceStatus", "operationEndTime"]
    try:
        pag = s.client("ssm", region_name=region).get_paginator(
            "describe_instance_patch_states")
        rows = []
        for page in pag.paginate():
            for st_ in page.get("InstancePatchStates", []):
                rows.append({
                    "instanceId": st_.get("InstanceId"),
                    "patchGroup": st_.get("PatchGroup"),
                    "missingCount": st_.get("MissingCount", 0),
                    "failedCount": st_.get("FailedCount", 0),
                    "complianceStatus": "NON_COMPLIANT" if (
                        st_.get("MissingCount", 0) or st_.get("FailedCount", 0)
                    ) else "COMPLIANT",
                    "operationEndTime": _fmt(st_.get("OperationEndTime")),
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"SSM Patch no disponible: {exc}")


def run_patch_baseline(s: boto3.Session, instance_ids: List[str], region: str) -> Dict:
    try:
        resp = s.client("ssm", region_name=region).send_command(
            InstanceIds=instance_ids,
            DocumentName="AWS-RunPatchBaseline",
            Parameters={"Operation": ["Install"]},
        )
        return {"ok": True, "message": f"Parcheo enviado (CommandId={resp['Command']['CommandId']})."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


def resolve_finding(s: boto3.Session, finding_id: str, product_arn: str,
                    status: str = "RESOLVED") -> Dict:
    try:
        s.client("securityhub").batch_update_findings(
            FindingIdentifiers=[{"Id": finding_id, "ProductArn": product_arn}],
            Workflow={"Status": status},
        )
        return {"ok": True, "message": f"Hallazgo marcado como {status}."}
    except ClientError as exc:
        _guard(exc)
        return {"ok": False, "message": str(exc)}


def well_architected_standards(s: boto3.Session) -> pd.DataFrame:
    """Estándares de Security Hub habilitados (Well-Architected posture)."""
    cols = ["standard", "status", "enabledAt"]
    try:
        subs = s.client("securityhub").get_enabled_standards().get("StandardsSubscriptions", [])
        rows = []
        for sub in subs:
            parts = (sub.get("StandardsArn") or "").split("/")
            rows.append({"standard": "/".join(parts[-2:]),
                         "status": sub.get("StandardsStatus"),
                         "enabledAt": _fmt(sub.get("UpdatedAt"))})
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        if exc.response["Error"]["Code"] in ("InvalidAccessException", "AccessDeniedException"):
            return _empty(cols, "Security Hub no habilitado.")
        raise


# ════════════════════════════════════════════════════════════════════════════
# 4. CICLO DE VIDA / DEPRECADOS
# ════════════════════════════════════════════════════════════════════════════
def deprecated_services(s: boto3.Session) -> pd.DataFrame:
    """AWS Health: eventos de retiro/deprecación (scheduledChange)."""
    cols = ["service", "eventTypeCode", "region", "statusCode",
            "startTime", "lastUpdatedTime"]
    try:
        pag = s.client("health", region_name="us-east-1").get_paginator("describe_events")
        rows = []
        keywords = ("retire", "deprecat", "end_of", "eol", "shutdown")
        for page in pag.paginate(
            filter={"eventTypeCategories": ["scheduledChange"],
                    "startTimes": [{"from": dt.datetime.utcnow() - dt.timedelta(days=90)}]},
            PaginationConfig={"MaxItems": 200},
        ):
            for ev in page.get("events", []):
                code = (ev.get("eventTypeCode") or "").lower()
                if any(k in code for k in keywords) or ev.get("eventTypeCategory") == "scheduledChange":
                    rows.append({
                        "service": ev.get("service"),
                        "eventTypeCode": ev.get("eventTypeCode"),
                        "region": ev.get("region"),
                        "statusCode": ev.get("statusCode"),
                        "startTime": _fmt(ev.get("startTime")),
                        "lastUpdatedTime": _fmt(ev.get("lastUpdatedTime")),
                    })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        if exc.response["Error"]["Code"] == "SubscriptionRequiredException":
            return _empty(cols, "AWS Health requiere plan Business/Enterprise.")
        raise


# ════════════════════════════════════════════════════════════════════════════
# Alarmas CloudWatch
# ════════════════════════════════════════════════════════════════════════════
def cloudwatch_alarms(s: boto3.Session, region: str) -> pd.DataFrame:
    cols = ["alarmName", "stateValue", "metricName",
            "namespace", "stateReason", "updatedAt"]
    try:
        pag = s.client("cloudwatch", region_name=region).get_paginator("describe_alarms")
        rows = []
        for page in pag.paginate(StateValue="ALARM"):
            for al in page.get("MetricAlarms", []):
                rows.append({"alarmName": al.get("AlarmName"), "stateValue": al.get("StateValue"),
                             "metricName": al.get("MetricName"), "namespace": al.get("Namespace"),
                             "stateReason": al.get("StateReason"),
                             "updatedAt": _fmt(al.get("StateUpdatedTimestamp"))})
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        raise


# ════════════════════════════════════════════════════════════════════════════
# INVENTARIO DE RECURSOS (módulo 1.2)
# ════════════════════════════════════════════════════════════════════════════

def inventory_ec2(s: boto3.Session, region: str) -> pd.DataFrame:
    """Inventario de instancias EC2 con estado, tipo y etiquetas."""
    cols = ["instanceId", "name", "state", "instanceType", "platform",
            "privateIp", "publicIp", "az", "launchTime"]
    try:
        ec2 = s.client("ec2", region_name=region)
        rows = []
        for reservation in ec2.describe_instances().get("Reservations", []):
            for inst in reservation.get("Instances", []):
                name = next((t["Value"] for t in inst.get("Tags", [])
                             if t["Key"] == "Name"), "—")
                rows.append({
                    "instanceId": inst.get("InstanceId"),
                    "name": name,
                    "state": inst.get("State", {}).get("Name"),
                    "instanceType": inst.get("InstanceType"),
                    "platform": inst.get("Platform", "linux"),
                    "privateIp": inst.get("PrivateIpAddress"),
                    "publicIp": inst.get("PublicIpAddress"),
                    "az": inst.get("Placement", {}).get("AvailabilityZone"),
                    "launchTime": _fmt(inst.get("LaunchTime")),
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"EC2 no accesible: {exc}")


def inventory_rds(s: boto3.Session, region: str) -> pd.DataFrame:
    """Inventario de instancias RDS."""
    cols = ["dbInstanceId", "engine", "engineVersion", "status",
            "instanceClass", "multiAZ", "storageGB", "endpoint"]
    try:
        rds = s.client("rds", region_name=region)
        rows = []
        for db in rds.describe_db_instances().get("DBInstances", []):
            ep = db.get("Endpoint") or {}
            rows.append({
                "dbInstanceId": db.get("DBInstanceIdentifier"),
                "engine": db.get("Engine"),
                "engineVersion": db.get("EngineVersion"),
                "status": db.get("DBInstanceStatus"),
                "instanceClass": db.get("DBInstanceClass"),
                "multiAZ": db.get("MultiAZ"),
                "storageGB": db.get("AllocatedStorage"),
                "endpoint": f"{ep.get('Address','')}:{ep.get('Port','')}",
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"RDS no accesible: {exc}")


def inventory_lambda(s: boto3.Session, region: str) -> pd.DataFrame:
    """Inventario de funciones Lambda."""
    cols = ["functionName", "runtime", "memorySize", "timeout",
            "lastModified", "codeSize", "state"]
    try:
        client = s.client("lambda", region_name=region)
        rows = []
        paginator = client.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                rows.append({
                    "functionName": fn.get("FunctionName"),
                    "runtime": fn.get("Runtime"),
                    "memorySize": fn.get("MemorySize"),
                    "timeout": fn.get("Timeout"),
                    "lastModified": fn.get("LastModified"),
                    "codeSize": fn.get("CodeSize"),
                    "state": fn.get("State", "Active"),
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"Lambda no accesible: {exc}")


def inventory_s3(s: boto3.Session) -> pd.DataFrame:
    """Inventario de buckets S3."""
    cols = ["bucketName", "creationDate", "region"]
    try:
        client = s.client("s3")
        buckets = client.list_buckets().get("Buckets", [])
        rows = []
        for b in buckets:
            try:
                loc = client.get_bucket_location(
                    Bucket=b["Name"])["LocationConstraint"] or "us-east-1"
            except ClientError:
                loc = "unknown"
            rows.append({
                "bucketName": b.get("Name"),
                "creationDate": _fmt(b.get("CreationDate")),
                "region": loc,
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"S3 no accesible: {exc}")


def inventory_elb(s: boto3.Session, region: str) -> pd.DataFrame:
    """Inventario de Load Balancers (ELBv2)."""
    cols = ["name", "type", "scheme", "state", "dnsName", "az"]
    try:
        client = s.client("elbv2", region_name=region)
        rows = []
        for lb in client.describe_load_balancers().get("LoadBalancers", []):
            rows.append({
                "name": lb.get("LoadBalancerName"),
                "type": lb.get("Type"),
                "scheme": lb.get("Scheme"),
                "state": lb.get("State", {}).get("Code"),
                "dnsName": lb.get("DNSName"),
                "az": ", ".join(
                    az.get("ZoneName", "") for az in lb.get("AvailabilityZones", [])),
            })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"ELB no accesible: {exc}")


# ── Métricas CloudWatch para un recurso ──────────────────────────────────────
def get_ec2_metrics(s: boto3.Session, instance_id: str, region: str,
                    hours: int = 3) -> pd.DataFrame:
    """
    CPU, Network In/Out y StatusCheckFailed para una instancia EC2.
    Devuelve puntos de datos de las últimas N horas con periodo de 5 min.
    """
    import datetime as dt
    cols = ["timestamp", "metric", "value", "unit"]
    try:
        cw = s.client("cloudwatch", region_name=region)
        end   = dt.datetime.utcnow()
        start = end - dt.timedelta(hours=hours)
        metrics_cfg = [
            ("CPUUtilization",   "AWS/EC2", "Percent"),
            ("NetworkIn",        "AWS/EC2", "Bytes"),
            ("NetworkOut",       "AWS/EC2", "Bytes"),
            ("StatusCheckFailed","AWS/EC2", "Count"),
        ]
        rows = []
        for metric_name, namespace, unit in metrics_cfg:
            resp = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric_name,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start, EndTime=end, Period=300, Statistics=["Average"])
            for dp in sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"]):
                rows.append({
                    "timestamp": dp["Timestamp"].isoformat(),
                    "metric": metric_name,
                    "value": round(dp.get("Average", 0), 4),
                    "unit": unit,
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"Metricas EC2 no accesibles: {exc}")


def get_rds_metrics(s: boto3.Session, db_id: str, region: str,
                    hours: int = 3) -> pd.DataFrame:
    """CPU, FreeStorageSpace, DatabaseConnections para RDS."""
    import datetime as dt
    cols = ["timestamp", "metric", "value", "unit"]
    try:
        cw = s.client("cloudwatch", region_name=region)
        end   = dt.datetime.utcnow()
        start = end - dt.timedelta(hours=hours)
        metrics_cfg = [
            ("CPUUtilization",      "Percent"),
            ("FreeStorageSpace",    "Bytes"),
            ("DatabaseConnections", "Count"),
            ("ReadIOPS",            "Count/Second"),
            ("WriteIOPS",           "Count/Second"),
        ]
        rows = []
        for metric_name, unit in metrics_cfg:
            resp = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName=metric_name,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                StartTime=start, EndTime=end, Period=300, Statistics=["Average"])
            for dp in sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"]):
                rows.append({
                    "timestamp": dp["Timestamp"].isoformat(),
                    "metric": metric_name,
                    "value": round(dp.get("Average", 0), 4),
                    "unit": unit,
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"Metricas RDS no accesibles: {exc}")


def get_lambda_metrics(s: boto3.Session, function_name: str, region: str,
                       hours: int = 3) -> pd.DataFrame:
    """Invocaciones, Errors, Duration, Throttles para Lambda."""
    import datetime as dt
    cols = ["timestamp", "metric", "value", "unit"]
    try:
        cw = s.client("cloudwatch", region_name=region)
        end   = dt.datetime.utcnow()
        start = end - dt.timedelta(hours=hours)
        metrics_cfg = [
            ("Invocations", "Count"),
            ("Errors",      "Count"),
            ("Duration",    "Milliseconds"),
            ("Throttles",   "Count"),
        ]
        rows = []
        for metric_name, unit in metrics_cfg:
            resp = cw.get_metric_statistics(
                Namespace="AWS/Lambda", MetricName=metric_name,
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start, EndTime=end, Period=300,
                Statistics=["Sum" if metric_name in ("Invocations","Errors","Throttles")
                            else "Average"])
            for dp in sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"]):
                stat_key = "Sum" if metric_name in ("Invocations","Errors","Throttles") else "Average"
                rows.append({
                    "timestamp": dp["Timestamp"].isoformat(),
                    "metric": metric_name,
                    "value": round(dp.get(stat_key, 0), 4),
                    "unit": unit,
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"Metricas Lambda no accesibles: {exc}")


def get_cloudwatch_alarms_all(s: boto3.Session, region: str) -> pd.DataFrame:
    """
    Todas las alarmas de CloudWatch (cualquier estado) con detalle completo.
    Segun el manual: filtrar por Sev y Time Range para identificar anomalias.
    """
    cols = ["alarmName", "stateValue", "metricName", "namespace",
            "comparisonOperator", "threshold", "evaluationPeriods",
            "stateReason", "stateUpdatedTimestamp", "actionsEnabled"]
    try:
        cw = s.client("cloudwatch", region_name=region)
        paginator = cw.get_paginator("describe_alarms")
        rows = []
        for page in paginator.paginate():
            for al in page.get("MetricAlarms", []):
                rows.append({
                    "alarmName": al.get("AlarmName"),
                    "stateValue": al.get("StateValue"),
                    "metricName": al.get("MetricName"),
                    "namespace": al.get("Namespace"),
                    "comparisonOperator": al.get("ComparisonOperator"),
                    "threshold": al.get("Threshold"),
                    "evaluationPeriods": al.get("EvaluationPeriods"),
                    "stateReason": (al.get("StateReason") or "")[:120],
                    "stateUpdatedTimestamp": _fmt(al.get("StateUpdatedTimestamp")),
                    "actionsEnabled": al.get("ActionsEnabled"),
                })
        return pd.DataFrame(rows, columns=cols)
    except ClientError as exc:
        _guard(exc)
        return _empty(cols, f"CloudWatch Alarms no accesible: {exc}")
