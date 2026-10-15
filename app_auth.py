"""
app_auth.py
===========
Autenticacion de la PROPIA aplicacion (gate previo al login de nubes).

Backend de usuarios (en orden de prioridad):
  1. Base de datos (tabla users, hash bcrypt) — fuente principal en producción.
     Multiusuario con rol. Se auto-siembra un admin inicial en el primer arranque.
  2. Variables de entorno PLANADM_USER / PLANADM_PASSWORD (o _SHA256) — útil en
     contenedores sin DB o para el primer bootstrap.
  3. Usuario por defecto admin/planadm2026 — SOLO para arrancar en local; cambiar.

La contraseña nunca se guarda en claro: en DB se usa bcrypt; en el fallback de
env/default se compara por SHA-256 (compatibilidad con la versión previa).

verify_user() es el único punto que consume app.py: su firma no cambia, así que
el resto de la aplicación no se toca.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

try:
    import bcrypt
    _BCRYPT = True
except Exception:  # noqa: BLE001
    _BCRYPT = False

try:
    import db as _db
except Exception:  # noqa: BLE001
    _db = None


_DEFAULT_USER = "admin"
_DEFAULT_PWD = "planadm2026"
_DEFAULT_PWD_SHA256 = hashlib.sha256(_DEFAULT_PWD.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash bcrypt (para almacenar en DB)."""
    if _BCRYPT:
        return bcrypt.hashpw(password.encode("utf-8"),
                             bcrypt.gensalt()).decode("utf-8")
    # Fallback si bcrypt no está: sha256 con prefijo marcador.
    return "sha256$" + _sha256(password)


def _check_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith("sha256$"):
        return _sha256(password) == stored_hash[len("sha256$"):]
    if _BCRYPT and stored_hash.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"),
                                  stored_hash.encode("utf-8"))
        except Exception:  # noqa: BLE001
            return False
    # Compatibilidad: hash sha256 "pelado" (env/default).
    return _sha256(password) == stored_hash


# ---------------------------------------------------------------------------
# Semilla del admin inicial (idempotente)
# ---------------------------------------------------------------------------
_SEEDED = False


def ensure_seed() -> None:
    """Crea el admin inicial en la DB si no hay usuarios todavía.

    Usuario/contraseña del seed: PLANADM_USER/PLANADM_PASSWORD si están en el
    entorno; si no, admin/planadm2026 (cámbialo tras el primer login)."""
    global _SEEDED
    if _SEEDED or _db is None:
        return
    try:
        if not _db.available():
            return
        if _db.count_users() == 0:
            user = os.environ.get("PLANADM_USER", _DEFAULT_USER)
            pwd = os.environ.get("PLANADM_PASSWORD", _DEFAULT_PWD)
            _db.upsert_user(user, hash_password(pwd), role="admin",
                            display_name="Administrador")
        _SEEDED = True
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Verificación
# ---------------------------------------------------------------------------
def _env_credentials() -> dict:
    """Credenciales desde variables de entorno (fallback sin DB)."""
    env_user = os.environ.get("PLANADM_USER")
    if env_user:
        env_hash = os.environ.get("PLANADM_PASSWORD_SHA256")
        if not env_hash and os.environ.get("PLANADM_PASSWORD"):
            env_hash = _sha256(os.environ["PLANADM_PASSWORD"])
        if env_hash:
            return {env_user: env_hash}
    return {}


def verify_user(username: str, password: str) -> bool:
    """True si usuario+contraseña son válidos. DB primero, luego env, luego
    usuario por defecto. Firma estable (la consume app.py)."""
    username = (username or "").strip()
    if not username:
        return False

    # 1) Base de datos (fuente principal).
    if _db is not None:
        ensure_seed()
        rec = _db.get_user(username)
        if rec and rec.get("active", True):
            if _check_password(password, rec.get("password_hash", "")):
                _db.touch_login(username)
                return True
            return False  # usuario existe en DB pero clave incorrecta

    # 2) Variables de entorno.
    env = _env_credentials()
    if username in env:
        return _sha256(password) == env[username]

    # 3) Usuario por defecto (solo si no hay DB ni env configurados).
    if not env and (_db is None or not _db.available()):
        if username == _DEFAULT_USER:
            return _sha256(password) == _DEFAULT_PWD_SHA256

    return False


def get_role(username: str) -> str:
    """Rol del usuario (admin|user). 'user' por defecto."""
    if _db is not None:
        rec = _db.get_user((username or "").strip())
        if rec:
            return rec.get("role", "user")
    if (username or "").strip() == _DEFAULT_USER:
        return "admin"
    return "user"


def default_hint() -> Optional[str]:
    """Pista de credenciales solo si seguimos con el usuario por defecto."""
    # Si hay usuarios en DB o env configurado, no mostrar pista.
    if _db is not None and _db.available() and _db.count_users() > 0:
        # ¿El unico usuario es el admin por defecto con la clave por defecto?
        rec = _db.get_user(_DEFAULT_USER)
        if rec and _check_password(_DEFAULT_PWD, rec.get("password_hash", "")) \
                and _db.count_users() == 1:
            return ("Usuario por defecto: admin · contraseña: planadm2026 "
                    "(créalo/cámbialo cuanto antes).")
        return None
    if _env_credentials():
        return None
    return ("Usuario por defecto: admin · contraseña: planadm2026 "
            "(configúralo en la base de datos o variables de entorno).")


# ---------------------------------------------------------------------------
# Gestión de usuarios (para un futuro panel de admin)
# ---------------------------------------------------------------------------
def create_user(username: str, password: str, role: str = "user",
                display_name: str = "") -> bool:
    if _db is None or not _db.available():
        return False
    return _db.upsert_user(username.strip(), hash_password(password),
                           role=role, display_name=display_name)
