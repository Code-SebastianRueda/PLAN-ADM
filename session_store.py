"""
session_store.py
================
Persistencia de la sesión de nube de PLAN-ADM entre recargas del navegador.

Backend conmutable (en orden de preferencia), elegido en tiempo de ejecución:
  1. Redis    si REDIS_URL está definida (ideal en producción multi-réplica).
  2. Base de datos (tabla app_sessions) si la DB está disponible.
  3. Archivo local (.plan_adm_session.json) como fallback (desarrollo).

La API pública NO cambia respecto a la versión anterior, así que app.py no se
toca: save_azure / load_azure / drop_azure / save_aws / load_aws / drop_aws / clear.

Contenido (igual que antes):
    Azure -> AuthenticationRecord serializado + account_hint + tenants.
    AWS   -> accessToken del portal SSO + metadatos + expiración.

Guarda material sensible (tokens): el backend debe tratarse como confidencial.
El 'owner' identifica a quién pertenece la sesión; por ahora es un valor único
(se enlazará al usuario autenticado cuando haya multiusuario pleno).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

_STORE_PATH = Path(__file__).parent / ".plan_adm_session.json"
_OWNER = os.environ.get("PLANADM_SESSION_OWNER", "default")
_REDIS_KEY = f"planadm:session:{_OWNER}"

# ---------------------------------------------------------------------------
# Selección de backend
# ---------------------------------------------------------------------------
_redis = None
_redis_tried = False


def _get_redis():
    global _redis, _redis_tried
    if _redis_tried:
        return _redis
    _redis_tried = True
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis as _r
        client = _r.from_url(url, decode_responses=True,
                             socket_connect_timeout=2)
        client.ping()
        _redis = client
    except Exception:  # noqa: BLE001
        _redis = None
    return _redis


def _db_available() -> bool:
    try:
        import db as _db
        return _db.available()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Lectura/escritura del dict completo según backend
# ---------------------------------------------------------------------------
def _read_all() -> dict:
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(_REDIS_KEY)
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            pass
    if _db_available():
        try:
            import db as _db
            out = {}
            for cloud in ("azure", "aws"):
                payload = _db.session_get(_OWNER, cloud)
                if payload:
                    out[cloud] = payload
            return out
        except Exception:  # noqa: BLE001
            pass
    # Fallback archivo.
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_cloud(cloud: str, payload: Optional[dict]) -> None:
    """Escribe (o borra si payload=None) la sección de una nube."""
    r = _get_redis()
    if r is not None:
        try:
            data = _read_all()
            if payload is None:
                data.pop(cloud, None)
            else:
                data[cloud] = payload
            r.set(_REDIS_KEY, json.dumps(data))
            return
        except Exception:  # noqa: BLE001
            pass
    if _db_available():
        try:
            import db as _db
            if payload is None:
                _db.session_clear(_OWNER, cloud)
            else:
                _db.session_set(_OWNER, cloud, payload)
            return
        except Exception:  # noqa: BLE001
            pass
    # Fallback archivo.
    try:
        data = {}
        try:
            data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        if payload is None:
            data.pop(cloud, None)
        else:
            data[cloud] = payload
        _STORE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def clear() -> None:
    """Elimina por completo la sesión persistida (usar en logout)."""
    r = _get_redis()
    if r is not None:
        try:
            r.delete(_REDIS_KEY)
        except Exception:  # noqa: BLE001
            pass
    if _db_available():
        try:
            import db as _db
            _db.session_clear(_OWNER)
        except Exception:  # noqa: BLE001
            pass
    try:
        _STORE_PATH.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------
def save_azure(record_serialized: str, account_hint: str = "",
               tenants: Optional[list] = None) -> None:
    _write_cloud("azure", {
        "record": record_serialized,
        "account_hint": account_hint or "",
        "tenants": tenants or [],
    })


def load_azure() -> Optional[dict]:
    az = _read_all().get("azure")
    if az and az.get("record"):
        return az
    return None


def drop_azure() -> None:
    _write_cloud("azure", None)


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
def save_aws(access_token: str, start_url: str, sso_region: str,
             client_name: str, expires_at: float) -> None:
    _write_cloud("aws", {
        "access_token": access_token,
        "start_url": start_url,
        "sso_region": sso_region,
        "client_name": client_name,
        "expires_at": float(expires_at or 0),
    })


def load_aws(skew_seconds: int = 60) -> Optional[dict]:
    aws = _read_all().get("aws")
    if not aws or not aws.get("access_token"):
        return None
    if float(aws.get("expires_at", 0)) <= time.time() + skew_seconds:
        drop_aws()
        return None
    return aws


def drop_aws() -> None:
    _write_cloud("aws", None)


# ---------------------------------------------------------------------------
# Diagnóstico de backend (opcional, para UI/logs)
# ---------------------------------------------------------------------------
def backend_name() -> str:
    if _get_redis() is not None:
        return "redis"
    if _db_available():
        return "database"
    return "file"
