"""
secrets_provider.py
===================
Punto ÚNICO para obtener secretos/credenciales de nubes y configuración sensible.

Jerarquía de fuentes (primera que responda gana):
  1. AWS Secrets Manager  — si SECRETS_BACKEND=aws (o se detecta en EC2 con rol).
     Lee un secreto JSON (nombre en SECRETS_NAME, por defecto 'planadm/secrets')
     y expone sus claves. Ideal en producción: nada sensible en el repo ni en el
     contenedor; la EC2 usa su IAM role para leerlo.
  2. Variables de entorno — siempre consultadas como fallback.
  3. Archivo .env local   — si existe, se carga a env al importar (solo dev).

NO se guardan secretos en el código. Para desarrollo local usa .env (gitignored).
Para producción, define SECRETS_BACKEND=aws y el IAM role de la EC2 con permiso
secretsmanager:GetSecretValue sobre el secreto.

Uso típico:
    import secrets_provider as secrets
    tenant = secrets.get("AZURE_TENANT_ID")
    aws_key = secrets.get("AWS_ACCESS_KEY_ID", default="")
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

_ENV_FILE = Path(__file__).parent / ".env"


# ---------------------------------------------------------------------------
# Carga de .env local (solo desarrollo)
# ---------------------------------------------------------------------------
def _load_dotenv_once() -> None:
    if not _ENV_FILE.exists():
        return
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # No pisar variables ya definidas en el entorno real.
            os.environ.setdefault(key, val)
    except Exception:  # noqa: BLE001
        pass


_load_dotenv_once()


# ---------------------------------------------------------------------------
# AWS Secrets Manager
# ---------------------------------------------------------------------------
def _use_aws() -> bool:
    backend = os.environ.get("SECRETS_BACKEND", "").lower()
    return backend == "aws"


@lru_cache(maxsize=1)
def _aws_secret_dict() -> dict:
    """Lee el secreto JSON de AWS Secrets Manager (cacheado). {} si falla."""
    if not _use_aws():
        return {}
    name = os.environ.get("SECRETS_NAME", "planadm/secrets")
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=name)
        raw = resp.get("SecretString") or ""
        return json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[secrets] No se pudo leer AWS Secrets Manager '{name}': "
              f"{type(exc).__name__}. Se usará el entorno.")
        return {}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def get(key: str, default: Optional[str] = None) -> Optional[str]:
    """Devuelve el secreto por nombre. Orden: Secrets Manager -> env -> default."""
    aws = _aws_secret_dict()
    if key in aws and aws[key] not in (None, ""):
        return str(aws[key])
    val = os.environ.get(key)
    if val not in (None, ""):
        return val
    return default


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on", "si", "sí")


def all_keys() -> list:
    """Nombres de secretos disponibles (de Secrets Manager + env relevantes)."""
    keys = set(_aws_secret_dict().keys())
    for k in os.environ:
        if k.startswith(("AZURE_", "AWS_", "GCP_", "OCI_", "IBM_", "PLANADM_")):
            keys.add(k)
    return sorted(keys)


def backend_name() -> str:
    return "aws-secrets-manager" if _use_aws() else (
        "dotenv+env" if _ENV_FILE.exists() else "env")


# ---------------------------------------------------------------------------
# Helpers específicos por nube (comodidad; devuelven dicts listos para usar)
# ---------------------------------------------------------------------------
def azure_service_principal() -> dict:
    """Credenciales de Service Principal de Azure (para modo desatendido).

    Vacío si no están configuradas (la app seguirá con device-code interactivo)."""
    tid = get("AZURE_TENANT_ID")
    cid = get("AZURE_CLIENT_ID")
    sec = get("AZURE_CLIENT_SECRET")
    if tid and cid and sec:
        return {"tenant_id": tid, "client_id": cid, "client_secret": sec}
    return {}


def aws_credentials() -> dict:
    """Access key de AWS si está configurada (para integración futura)."""
    ak = get("AWS_ACCESS_KEY_ID")
    sk = get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        return {"access_key_id": ak, "secret_access_key": sk,
                "region": get("AWS_REGION", "us-east-1")}
    return {}
