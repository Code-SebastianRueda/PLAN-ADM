"""
data_cache.py
=============
Caché DIARIA persistente por área, respaldada en la base de datos.

Objetivo (requerimiento de negocio):
  - Las áreas 'pesadas' (costos, seguridad, inventario, deprecados, backups) se
    consultan a Azure UNA VEZ AL DÍA por alcance (tenant+suscripciones) y el
    resultado se guarda en la DB. Durante el resto del día, la app sirve el
    snapshot guardado en vez de volver a consultar Azure.
  - ALERTAMIENTO (1.5) NO usa esta caché: siempre consulta en tiempo real.

Mecanismo:
  get_or_fetch(area, scope_key, fetch_fn, max_age_hours=24)
    1. Busca el snapshot más reciente de (area, scope_key) en la DB.
    2. Si existe y es más nuevo que max_age_hours -> lo devuelve (sin tocar Azure).
    3. Si no -> ejecuta fetch_fn(), guarda el resultado y lo devuelve.
    force=True ignora la caché (botón "Actualizar").

Serialización:
  Los datos de cada área suelen ser DataFrames (o dicts con DataFrames). Se
  serializan con pickle+base64 y se guardan como JSON en history_snapshots.metrics,
  junto con unos KPIs legibles para tendencias históricas.

Si la DB no está disponible, degrada con gracia: ejecuta fetch_fn directamente
(sin persistir), de modo que la app sigue funcionando.
"""

from __future__ import annotations

import base64
import datetime as dt
import pickle
from typing import Callable, Optional

try:
    import db as _db
except Exception:  # noqa: BLE001
    _db = None


# Áreas que SÍ usan caché diaria. 'alertas' queda excluida a propósito.
DAILY_AREAS = {"costos", "seguridad", "inventario", "deprecados", "backups"}


def _encode(obj) -> str:
    return base64.b64encode(pickle.dumps(obj)).decode("ascii")


def _decode(s: str):
    return pickle.loads(base64.b64decode(s.encode("ascii")))


def _latest_fresh(area: str, scope_key: str, max_age_hours: float):
    """Devuelve (payload, captured_at) del snapshot fresco, o (None, None)."""
    if _db is None or not _db.available():
        return None, None
    snaps = _db.load_snapshots(area, scope=scope_key, limit=1)
    if not snaps:
        return None, None
    snap = snaps[0]
    captured = snap.get("captured_at", "")
    try:
        cap_dt = dt.datetime.fromisoformat(captured)
    except Exception:  # noqa: BLE001
        return None, None
    age_h = (dt.datetime.utcnow() - cap_dt.replace(tzinfo=None)).total_seconds() / 3600
    if age_h > max_age_hours:
        return None, None
    metrics = snap.get("metrics", {}) or {}
    blob = metrics.get("_blob")
    if not blob:
        return None, None
    try:
        return _decode(blob), captured
    except Exception:  # noqa: BLE001
        return None, None


def get_or_fetch(area: str, scope_key: str, fetch_fn: Callable,
                 kpi_fn: Optional[Callable] = None,
                 max_age_hours: float = 24.0, force: bool = False,
                 cloud: str = "Azure") -> tuple:
    """Devuelve (data, source, captured_at).

    source: 'cache' si vino de la DB, 'fresh' si se consultó Azure ahora.
    kpi_fn(data) -> dict con KPIs legibles para guardar aparte (tendencias).
    """
    # 1) Intentar caché (si no se fuerza).
    if not force:
        payload, captured = _latest_fresh(area, scope_key, max_age_hours)
        if payload is not None:
            return payload, "cache", captured

    # 2) Consultar en vivo.
    data = fetch_fn()

    # 3) Persistir (si hay DB). Nunca romper la app por un fallo de guardado.
    if _db is not None and _db.available():
        try:
            metrics = {"_blob": _encode(data)}
            if kpi_fn is not None:
                try:
                    metrics["kpis"] = kpi_fn(data)
                except Exception:  # noqa: BLE001
                    pass
            _db.save_snapshot(area, metrics, cloud=cloud, scope=scope_key)
        except Exception:  # noqa: BLE001
            pass

    return data, "fresh", dt.datetime.utcnow().isoformat()


def last_captured(area: str, scope_key: str) -> Optional[str]:
    """Fecha ISO del último snapshot guardado para (area, scope), o None."""
    if _db is None or not _db.available():
        return None
    snaps = _db.load_snapshots(area, scope=scope_key, limit=1)
    return snaps[0].get("captured_at") if snaps else None


def kpi_history(area: str, scope_key: str, limit: int = 90) -> list:
    """Serie de KPIs históricos [{captured_at, kpis}] para graficar tendencias."""
    if _db is None or not _db.available():
        return []
    out = []
    for snap in _db.load_snapshots(area, scope=scope_key, limit=limit):
        kpis = (snap.get("metrics", {}) or {}).get("kpis")
        if kpis:
            out.append({"captured_at": snap.get("captured_at"), "kpis": kpis})
    return list(reversed(out))  # cronológico ascendente
