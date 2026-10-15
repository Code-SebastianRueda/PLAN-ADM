"""
db.py
=====
Capa de datos de PLAN-ADM (SQLAlchemy 2.x).

Un solo punto para la base de datos, con backend dual:
  - PRODUCCIÓN: PostgreSQL si DATABASE_URL está definida
    (ej. postgresql+psycopg2://user:pass@host:5432/planadm).
  - LOCAL / fallback: SQLite en un archivo del proyecto, para que todo funcione
    sin instalar Postgres y no romper el flujo actual.

Tablas:
  users             -> usuarios de la app (gate), con hash bcrypt y rol.
  app_sessions      -> sesión de nube persistida (reemplaza el .json local).
  cloud_connections -> metadatos de conexiones por usuario/nube (futuro).
  history_snapshots -> históricos (costos, alertas, seguridad, etc.) para
                       tendencias a lo largo del tiempo.

Todo es idempotente: init_db() crea las tablas si no existen y no borra datos.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    JSON, Column, DateTime, Integer, String, Text, Boolean, Float,
    create_engine, select, delete, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# ---------------------------------------------------------------------------
# URL de conexión: Postgres si DATABASE_URL; si no, SQLite local.
# ---------------------------------------------------------------------------
_DEFAULT_SQLITE = Path(__file__).parent / "planadm.db"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Normalizar el prefijo heredado 'postgres://' -> 'postgresql+psycopg2://'.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url
    return f"sqlite:///{_DEFAULT_SQLITE}"


_ENGINE = None
_SessionLocal = None


def get_engine():
    global _ENGINE, _SessionLocal
    if _ENGINE is None:
        url = _database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _ENGINE = create_engine(url, pool_pre_ping=True, future=True,
                                connect_args=connect_args)
        _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False,
                                     future=True)
    return _ENGINE


def get_session():
    if _SessionLocal is None:
        get_engine()
    return _SessionLocal()


def is_postgres() -> bool:
    return _database_url().startswith("postgresql")


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(120), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)   # bcrypt
    role = Column(String(40), default="user")             # admin | user
    display_name = Column(String(160), default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class AppSession(Base):
    """Sesión de nube persistida (reemplaza .plan_adm_session.json).

    Una fila por 'owner' (por ahora un valor fijo o el usuario). El payload
    guarda el mismo JSON que antes iba al archivo."""
    __tablename__ = "app_sessions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner = Column(String(120), nullable=False, index=True)
    cloud = Column(String(40), nullable=False)            # azure | aws
    payload = Column(Text, nullable=False)                # JSON serializado
    updated_at = Column(DateTime, default=dt.datetime.utcnow,
                        onupdate=dt.datetime.utcnow)
    __table_args__ = (UniqueConstraint("owner", "cloud", name="uq_session_owner_cloud"),)


class CloudConnection(Base):
    """Metadatos de conexiones por usuario/nube (para multicliente futuro)."""
    __tablename__ = "cloud_connections"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner = Column(String(120), nullable=False, index=True)
    cloud = Column(String(40), nullable=False)
    client_name = Column(String(160), default="")   # tenant/cliente
    scope_id = Column(String(200), default="")       # tenantId / accountId
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class HistorySnapshot(Base):
    """Históricos para tendencias. Un snapshot por (area, scope, fecha)."""
    __tablename__ = "history_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    area = Column(String(40), nullable=False, index=True)   # costos|alertas|seguridad|backups|inventario|deprecados
    cloud = Column(String(40), default="Azure")
    scope = Column(String(200), default="")                 # tenant/sub key
    captured_at = Column(DateTime, default=dt.datetime.utcnow, index=True)
    metrics = Column(JSON, nullable=False)                  # KPIs del momento
    __table_args__ = (Index("ix_history_area_time", "area", "captured_at"),)


# ---------------------------------------------------------------------------
# Inicialización
# ---------------------------------------------------------------------------
_INITIALIZED = False


def init_db() -> bool:
    """Crea las tablas si no existen. Idempotente. Devuelve True si la DB
    está disponible, False si falló (para degradar con gracia)."""
    global _INITIALIZED
    if _INITIALIZED:
        return True
    try:
        engine = get_engine()
        Base.metadata.create_all(engine)
        _INITIALIZED = True
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[db] init_db falló ({type(exc).__name__}): {exc}. "
              "La app seguirá con backends locales (archivo).")
        return False


def available() -> bool:
    """True si la DB responde (para decidir backend de auth/sesiones)."""
    try:
        if not init_db():
            return False
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Helpers de alto nivel (usados por app_auth, session_store, históricos)
# ---------------------------------------------------------------------------
def get_user(username: str) -> Optional[dict]:
    try:
        with get_session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if not u:
                return None
            return {"username": u.username, "password_hash": u.password_hash,
                    "role": u.role, "display_name": u.display_name,
                    "active": u.active}
    except Exception:  # noqa: BLE001
        return None


def upsert_user(username: str, password_hash: str, role: str = "user",
                display_name: str = "") -> bool:
    try:
        with get_session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if u:
                u.password_hash = password_hash
                u.role = role
                if display_name:
                    u.display_name = display_name
            else:
                s.add(User(username=username, password_hash=password_hash,
                           role=role, display_name=display_name))
            s.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[db] upsert_user falló: {exc}")
        return False


def count_users() -> int:
    try:
        with get_session() as s:
            return s.query(User).count()
    except Exception:  # noqa: BLE001
        return 0


def list_users() -> list:
    """Lista todos los usuarios (sin el hash de contraseña)."""
    try:
        with get_session() as s:
            rows = s.scalars(select(User).order_by(User.username)).all()
            return [{"username": u.username, "role": u.role,
                     "display_name": u.display_name or "", "active": u.active,
                     "last_login": (u.last_login.isoformat()
                                    if u.last_login else None)}
                    for u in rows]
    except Exception:  # noqa: BLE001
        return []


def set_user_active(username: str, active: bool) -> bool:
    """Activa o desactiva un usuario (bloquea el login sin borrarlo)."""
    try:
        with get_session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if not u:
                return False
            u.active = active
            s.commit()
            return True
    except Exception:  # noqa: BLE001
        return False


def delete_user(username: str) -> bool:
    """Elimina un usuario definitivamente."""
    try:
        with get_session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if not u:
                return False
            s.delete(u)
            s.commit()
            return True
    except Exception:  # noqa: BLE001
        return False


def touch_login(username: str) -> None:
    try:
        with get_session() as s:
            u = s.scalar(select(User).where(User.username == username))
            if u:
                u.last_login = dt.datetime.utcnow()
                s.commit()
    except Exception:  # noqa: BLE001
        pass


# ---- Sesiones de nube (payload JSON por owner+cloud) ----
def session_get(owner: str, cloud: str) -> Optional[dict]:
    try:
        with get_session() as s:
            row = s.scalar(select(AppSession).where(
                AppSession.owner == owner, AppSession.cloud == cloud))
            return json.loads(row.payload) if row else None
    except Exception:  # noqa: BLE001
        return None


def session_set(owner: str, cloud: str, payload: dict) -> bool:
    try:
        with get_session() as s:
            row = s.scalar(select(AppSession).where(
                AppSession.owner == owner, AppSession.cloud == cloud))
            data = json.dumps(payload)
            if row:
                row.payload = data
            else:
                s.add(AppSession(owner=owner, cloud=cloud, payload=data))
            s.commit()
            return True
    except Exception:  # noqa: BLE001
        return False


def session_clear(owner: str, cloud: Optional[str] = None) -> None:
    try:
        with get_session() as s:
            q = delete(AppSession).where(AppSession.owner == owner)
            if cloud:
                q = q.where(AppSession.cloud == cloud)
            s.execute(q)
            s.commit()
    except Exception:  # noqa: BLE001
        pass


# ---- Históricos ----
def save_snapshot(area: str, metrics: dict, cloud: str = "Azure",
                  scope: str = "") -> bool:
    try:
        with get_session() as s:
            s.add(HistorySnapshot(area=area, cloud=cloud, scope=scope,
                                  metrics=metrics))
            s.commit()
            return True
    except Exception:  # noqa: BLE001
        return False


def load_snapshots(area: str, scope: str = "", limit: int = 90) -> list:
    try:
        with get_session() as s:
            q = select(HistorySnapshot).where(HistorySnapshot.area == area)
            if scope:
                q = q.where(HistorySnapshot.scope == scope)
            q = q.order_by(HistorySnapshot.captured_at.desc()).limit(limit)
            rows = s.scalars(q).all()
            return [{"captured_at": r.captured_at.isoformat(),
                     "cloud": r.cloud, "scope": r.scope, "metrics": r.metrics}
                    for r in rows]
    except Exception:  # noqa: BLE001
        return []
