"""
daily_refresh.py
================
Job de REFRESCO DIARIO (desatendido) de los snapshots de Kyndryl Orbit.

Objetivo:
  Precargar cada mañana (6:00 AM hora Colombia) los datos pesados de cada
  cliente, de modo que cuando el equipo entre (~8:00 AM) todo esté servido
  desde la caché diaria (DB) sin esperar consultas a Azure.

Áreas que precarga (las mismas con caché diaria):
  costos, seguridad, inventario, deprecados, backups.
  'alertamiento' se EXCLUYE a propósito (siempre en tiempo real).

Autenticación DESATENDIDA:
  Usa Service Principals por cliente (NO el device-code interactivo de la app).
  Cada cliente se define con tenant_id + client_id + client_secret, que deben
  venir de un lugar seguro (AWS Secrets Manager / variables de entorno / .env),
  NUNCA hardcodeados.

ESTADO ACTUAL: DESACTIVADO.
  La lista de clientes (_load_clients) está vacía hasta que tengamos los
  Service Principals de cada cliente. Mientras tanto, el job corre, no encuentra
  clientes y termina sin hacer nada (seguro de ejecutar). Cuando se agreguen los
  SP, el job empezará a precargar automáticamente.

Programación (6 AM Colombia = 11:00 UTC):
  - Linux/EC2 (cron):   0 11 * * *  cd /app && python daily_refresh.py
  - Docker:             servicio 'scheduler' en docker-compose (ver abajo).
  - Windows (pruebas):  python daily_refresh.py

Uso manual / prueba:
  python daily_refresh.py            # refresca todos los clientes configurados
  python daily_refresh.py --dry-run  # solo lista lo que haría, sin consultar
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from typing import Callable, List, Optional

# Módulos de la app (capa de datos y de nubes).
import db
import data_cache
import secrets_provider

# Áreas pesadas a precargar y su función de carga + KPIs.
# Cada entrada: (area, loader(cred, subs) -> data, kpi_fn(data) -> dict | None)
import azure_costs
import azure_security
import azure_inventory
import azure_deprecations
import azure_backups

_AREAS = [
    ("costos",     lambda cred, subs: azure_costs.load_costs(cred, subs, 30),
     lambda d: azure_costs.kpis(d)),
    ("seguridad",  lambda cred, subs: azure_security.load_security(cred, subs),
     lambda d: azure_security.kpis(d)),
    ("inventario", lambda cred, subs: azure_inventory.list_resources(cred, subs),
     lambda d: azure_inventory.kpis(d)),
    ("deprecados", lambda cred, subs: azure_deprecations.list_deprecations(cred, subs),
     lambda d: azure_deprecations.summary(d)),
    ("backups",    lambda cred, subs: azure_backups.load_backups(cred, subs, 30),
     lambda d: azure_backups.kpis(d)),
]


# ---------------------------------------------------------------------------
# Clientes a refrescar (Service Principals). VACÍO hasta tener los accesos.
# ---------------------------------------------------------------------------
def _load_clients() -> List[dict]:
    """Devuelve la lista de clientes a precargar.

    Fuente del listado (en orden):
      1. secret/env ORBIT_DAILY_CLIENTS: JSON con una lista de objetos
         [{"name","tenant_id","client_id","client_secret"}].
         Lo ideal es que esto venga de AWS Secrets Manager en producción.
      2. Vacío -> el job no hace nada (estado actual).

    Cada cliente requiere un Service Principal con permisos de lectura
    (Reader + Cost Management Reader + Security Reader) sobre sus suscripciones.
    """
    raw = secrets_provider.get("ORBIT_DAILY_CLIENTS", "")
    if not raw:
        return []
    try:
        clients = json.loads(raw)
        valid = []
        for c in clients:
            if all(c.get(k) for k in ("tenant_id", "client_id", "client_secret")):
                valid.append(c)
        return valid
    except Exception as exc:  # noqa: BLE001
        print(f"[daily_refresh] ORBIT_DAILY_CLIENTS inválido: {exc}")
        return []


# ---------------------------------------------------------------------------
# Autenticación desatendida (Service Principal) + descubrimiento de subs
# ---------------------------------------------------------------------------
def _sp_credential(client: dict):
    """Crea una credencial de Service Principal (desatendida). None si falla."""
    try:
        from azure.identity import ClientSecretCredential
        return ClientSecretCredential(
            tenant_id=client["tenant_id"],
            client_id=client["client_id"],
            client_secret=client["client_secret"],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[daily_refresh] No se pudo crear credencial SP para "
              f"{client.get('name','?')}: {exc}")
        return None


def _discover_subscriptions(cred, tenant_id: str) -> List[dict]:
    """Suscripciones accesibles por el SP en el tenant."""
    try:
        import azure_auth
        return azure_auth.list_subscriptions(cred, tenant_id)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Refresco
# ---------------------------------------------------------------------------
def refresh_client(client: dict, dry_run: bool = False) -> dict:
    """Precarga todas las áreas de un cliente. Devuelve un resumen."""
    name = client.get("name", client.get("tenant_id", "cliente"))
    result = {"client": name, "areas": {}, "subs": 0, "ok": False}

    cred = _sp_credential(client)
    if cred is None:
        result["error"] = "credencial inválida"
        return result

    subs = _discover_subscriptions(cred, client["tenant_id"])
    result["subs"] = len(subs)
    if not subs:
        result["error"] = "sin suscripciones accesibles"
        return result

    scope_key = "|".join(sorted(s["subscriptionId"] for s in subs))

    for area, loader, kpi_fn in _AREAS:
        if dry_run:
            result["areas"][area] = "dry-run"
            continue
        try:
            _data, src, _cap = data_cache.get_or_fetch(
                area=area, scope_key=scope_key,
                fetch_fn=lambda ld=loader: ld(cred, subs),
                kpi_fn=kpi_fn, force=True)   # force: siempre consulta y guarda
            result["areas"][area] = src
        except Exception as exc:  # noqa: BLE001
            result["areas"][area] = f"error: {type(exc).__name__}"
            print(f"[daily_refresh] {name}/{area} falló: {exc}")
    result["ok"] = True
    return result


def run(dry_run: bool = False) -> int:
    started = dt.datetime.now()
    print(f"[daily_refresh] Inicio {started.isoformat()} "
          f"(UTC {dt.datetime.utcnow().isoformat()})")
    db.init_db()

    clients = _load_clients()
    if not clients:
        print("[daily_refresh] No hay clientes configurados "
              "(ORBIT_DAILY_CLIENTS vacío). Nada que hacer. "
              "Define los Service Principals cuando estén disponibles.")
        return 0

    print(f"[daily_refresh] {len(clients)} cliente(s) a precargar"
          + (" [DRY-RUN]" if dry_run else ""))
    summaries = []
    for c in clients:
        print(f"  → {c.get('name', c['tenant_id'])}")
        summaries.append(refresh_client(c, dry_run=dry_run))

    ok = sum(1 for s in summaries if s.get("ok"))
    print(f"[daily_refresh] Fin. {ok}/{len(clients)} clientes OK. "
          f"Duración: {(dt.datetime.now()-started).total_seconds():.0f}s")
    for s in summaries:
        print("  ", json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresco diario de Orbit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Lista lo que haría sin consultar Azure.")
    args = parser.parse_args()
    try:
        sys.exit(run(dry_run=args.dry_run))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
