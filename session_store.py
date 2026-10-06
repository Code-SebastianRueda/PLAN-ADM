"""
session_store.py
================
Persistencia LIVIANA de la sesion de PLAN-ADM entre recargas del navegador.

Streamlit borra st.session_state cuando el usuario pulsa F5 / recarga la
pestana. Para no obligar a reautenticar, aqui se guarda en un archivo local
el estado MINIMO necesario para reanudar la sesion:

    Azure  -> el AuthenticationRecord serializado. La credencial se reconstruye
              con DeviceCodeCredential(authentication_record=...) apoyandose en
              la cache de token persistente (TokenCachePersistenceOptions), sin
              reabrir el navegador.
    AWS    -> el accessToken del portal SSO + metadatos (start_url, region,
              cliente) y su expiracion. Mientras el token siga vigente se
              reutiliza; si expiro, se descarta y se pide login de nuevo.

El archivo vive junto al proyecto y queda excluido por .gitignore. Contiene
tokens: trátalo como material sensible (no se registra en consola).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_STORE_PATH = Path(__file__).parent / ".plan_adm_session.json"


# ---------------------------------------------------------------------------
# IO base
# ---------------------------------------------------------------------------
def _read() -> dict:
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write(data: dict) -> None:
    try:
        _STORE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def clear() -> None:
    """Elimina por completo la sesion persistida (usar en logout)."""
    try:
        _STORE_PATH.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------
def save_azure(record_serialized: str, account_hint: str = "",
               tenants: Optional[list] = None) -> None:
    """Guarda el AuthenticationRecord (ya serializado a str) y datos de apoyo."""
    data = _read()
    data["azure"] = {
        "record": record_serialized,
        "account_hint": account_hint or "",
        "tenants": tenants or [],
    }
    _write(data)


def load_azure() -> Optional[dict]:
    """Devuelve {'record', 'account_hint', 'tenants'} o None si no hay."""
    data = _read()
    az = data.get("azure")
    if az and az.get("record"):
        return az
    return None


def drop_azure() -> None:
    data = _read()
    if "azure" in data:
        data.pop("azure", None)
        _write(data)


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
def save_aws(access_token: str, start_url: str, sso_region: str,
             client_name: str, expires_at: float) -> None:
    data = _read()
    data["aws"] = {
        "access_token": access_token,
        "start_url": start_url,
        "sso_region": sso_region,
        "client_name": client_name,
        "expires_at": float(expires_at or 0),
    }
    _write(data)


def load_aws(skew_seconds: int = 60) -> Optional[dict]:
    """Devuelve la sesion AWS si el token sigue vigente; si expiro, la descarta."""
    data = _read()
    aws = data.get("aws")
    if not aws or not aws.get("access_token"):
        return None
    if float(aws.get("expires_at", 0)) <= time.time() + skew_seconds:
        drop_aws()
        return None
    return aws


def drop_aws() -> None:
    data = _read()
    if "aws" in data:
        data.pop("aws", None)
        _write(data)
