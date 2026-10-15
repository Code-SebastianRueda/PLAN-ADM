"""
cache_store.py
=============
Caché opcional respaldada por Redis, con degradación a memoria del proceso.

Objetivo: en PRODUCCIÓN con varias réplicas (p. ej. detrás de un balanceador),
compartir resultados de consultas costosas a Azure entre réplicas y usuarios.
En LOCAL / una sola réplica, no se necesita Redis: el decorador cae a una caché
en memoria (equivalente funcional a st.cache_data) sin exigir nada extra.

Diseño:
  - Si REDIS_URL está definida y responde -> backend Redis (valores serializados
    con pickle, TTL por entrada).
  - Si no -> backend memoria (dict con expiración por TTL).

Uso:
    import cache_store
    @cache_store.cached(ttl=120)
    def consulta_costosa(a, b): ...

La clave se deriva del nombre de la función + args. NO cachear objetos no
serializables (credenciales, clientes). Para funciones que reciben la credencial,
pasa un identificador estable (p. ej. subs_key) y resuelve la credencial dentro.
"""

from __future__ import annotations

import functools
import hashlib
import os
import pickle
import time
from typing import Callable, Optional

_redis = None
_redis_tried = False
_MEM: dict = {}


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
        client = _r.from_url(url, socket_connect_timeout=2)
        client.ping()
        _redis = client
    except Exception:  # noqa: BLE001
        _redis = None
    return _redis


def backend_name() -> str:
    return "redis" if _get_redis() is not None else "memory"


def _make_key(prefix: str, args, kwargs) -> str:
    try:
        raw = pickle.dumps((args, sorted(kwargs.items())))
    except Exception:  # noqa: BLE001
        raw = repr((args, kwargs)).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:24]
    return f"planadm:cache:{prefix}:{digest}"


def get(key: str):
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(key)
            return pickle.loads(raw) if raw is not None else None
        except Exception:  # noqa: BLE001
            return None
    item = _MEM.get(key)
    if item is None:
        return None
    value, expires = item
    if expires and expires < time.time():
        _MEM.pop(key, None)
        return None
    return value


def set(key: str, value, ttl: int = 120) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.set(key, pickle.dumps(value), ex=ttl)
            return
        except Exception:  # noqa: BLE001
            pass
    _MEM[key] = (value, time.time() + ttl if ttl else 0)


def clear(prefix: Optional[str] = None) -> None:
    r = _get_redis()
    if r is not None:
        try:
            pattern = f"planadm:cache:{prefix}:*" if prefix else "planadm:cache:*"
            for k in r.scan_iter(pattern):
                r.delete(k)
        except Exception:  # noqa: BLE001
            pass
    if prefix:
        for k in list(_MEM):
            if k.startswith(f"planadm:cache:{prefix}:"):
                _MEM.pop(k, None)
    else:
        _MEM.clear()


def cached(ttl: int = 120, prefix: Optional[str] = None) -> Callable:
    """Decorador: cachea el resultado (Redis o memoria) por `ttl` segundos."""
    def _decorator(fn: Callable) -> Callable:
        pfx = prefix or fn.__name__

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            key = _make_key(pfx, args, kwargs)
            hit = get(key)
            if hit is not None:
                return hit
            result = fn(*args, **kwargs)
            try:
                set(key, result, ttl=ttl)
            except Exception:  # noqa: BLE001
                pass
            return result

        _wrapper.cache_clear = lambda: clear(pfx)  # type: ignore[attr-defined]
        return _wrapper
    return _decorator
