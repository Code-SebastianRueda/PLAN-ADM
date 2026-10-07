"""
app_auth.py
===========
Autenticacion de la PROPIA aplicacion (gate previo al login de nubes).

TEMPORAL / fase actual: un unico usuario para operar la app. Tras pasar este
gate, la app pide el MFA de Azure (device code) como siguiente paso.

VISION FUTURA (no implementada aun): un directorio de usuarios de la app, cada
uno con los clientes/nubes que tiene autorizados. La funcion verify_user() es el
unico punto a cambiar para pasar de "un usuario" a "varios usuarios con RBAC".

Las credenciales NO se hardcodean. Se leen, en este orden:
  1. st.secrets["app_users"]  (archivo .streamlit/secrets.toml, gitignored)
  2. variables de entorno PLANADM_USER / PLANADM_PASSWORD_SHA256
  3. valor por defecto de arranque (solo para primer uso; cambialo cuanto antes)

La contraseña se compara por hash SHA-256; nunca se guarda en claro.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

try:
    import streamlit as st
    _ST = True
except Exception:  # noqa: BLE001
    _ST = False


# Usuario por defecto para el primer arranque. Cambialo definiendo secrets o
# variables de entorno (ver abajo). La contraseña por defecto es "planadm2026".
_DEFAULT_USER = "admin"
_DEFAULT_PWD_SHA256 = hashlib.sha256("planadm2026".encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _load_credentials() -> dict:
    """Devuelve {usuario: sha256_password}. Fuentes en orden de prioridad.

    - st.secrets["app_users"]: tabla usuario -> hash sha256 (recomendado).
      Ejemplo en .streamlit/secrets.toml:
        [app_users]
        admin = "<sha256 de la contraseña>"
    - env PLANADM_USER + PLANADM_PASSWORD_SHA256 (o PLANADM_PASSWORD en claro).
    - usuario por defecto (solo para arrancar).
    """
    # 1) st.secrets
    if _ST:
        try:
            users = st.secrets.get("app_users", None)
            if users:
                return {str(u): str(h) for u, h in dict(users).items()}
        except Exception:  # noqa: BLE001
            pass
    # 2) variables de entorno
    env_user = os.environ.get("PLANADM_USER")
    if env_user:
        env_hash = os.environ.get("PLANADM_PASSWORD_SHA256")
        if not env_hash and os.environ.get("PLANADM_PASSWORD"):
            env_hash = _sha256(os.environ["PLANADM_PASSWORD"])
        if env_hash:
            return {env_user: env_hash}
    # 3) por defecto
    return {_DEFAULT_USER: _DEFAULT_PWD_SHA256}


def verify_user(username: str, password: str) -> bool:
    """True si usuario+contraseña son validos. Comparacion por hash."""
    creds = _load_credentials()
    expected = creds.get((username or "").strip())
    if not expected:
        return False
    return _sha256(password) == expected


def default_hint() -> Optional[str]:
    """Pista para el primer arranque (solo si se usa el usuario por defecto)."""
    creds = _load_credentials()
    if creds == {_DEFAULT_USER: _DEFAULT_PWD_SHA256}:
        return ("Usuario por defecto: admin · contraseña: planadm2026 "
                "(configúralo en .streamlit/secrets.toml o variables de entorno).")
    return None
