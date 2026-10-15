"""
scheduler.py
============
Planificador liviano (sin cron del sistema) para el refresco diario de Orbit.

Se ejecuta como un proceso de larga duración dentro del contenedor 'scheduler'
(ver docker-compose.yml). Cada día, a la hora objetivo en UTC, invoca
daily_refresh.run().

Hora objetivo:
  6:00 AM hora Colombia (America/Bogota, UTC-5, sin horario de verano)
  = 11:00 UTC.
  Configurable con la variable de entorno ORBIT_REFRESH_UTC_HOUR (0-23),
  por defecto 11. Minuto con ORBIT_REFRESH_UTC_MINUTE (por defecto 0).

Por qué un bucle en Python y no cron:
  - El contenedor corre como usuario no-root (appuser); cron del sistema
    requeriría root y configuración extra.
  - Un bucle simple es más fácil de observar en los logs de 'docker compose'.
  - En EC2 sin Docker, también puede usarse cron directamente:
        0 11 * * *  cd /app && python daily_refresh.py
    (ver cabecera de daily_refresh.py).

Estado: ACTIVO como proceso, pero inofensivo mientras daily_refresh no tenga
clientes configurados (ORBIT_DAILY_CLIENTS vacío) — solo registrará un no-op.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import traceback

import daily_refresh


def _target() -> tuple[int, int]:
    hour = int(os.getenv("ORBIT_REFRESH_UTC_HOUR", "11"))
    minute = int(os.getenv("ORBIT_REFRESH_UTC_MINUTE", "0"))
    return max(0, min(23, hour)), max(0, min(59, minute))


def _seconds_until_next_run(now: dt.datetime, hour: int, minute: int) -> float:
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def main() -> None:
    hour, minute = _target()
    print(f"[scheduler] Iniciado. Refresco diario a las "
          f"{hour:02d}:{minute:02d} UTC (06:00 America/Bogota). "
          f"ORBIT_DAILY_CLIENTS controla si hace trabajo real.")
    while True:
        now = dt.datetime.utcnow()
        wait = _seconds_until_next_run(now, hour, minute)
        nxt = now + dt.timedelta(seconds=wait)
        print(f"[scheduler] Próxima ejecución: {nxt.isoformat()}Z "
              f"(en {wait/3600:.1f} h)")
        time.sleep(wait)
        try:
            print("[scheduler] Lanzando refresco diario...")
            daily_refresh.run(dry_run=False)
        except Exception:  # noqa: BLE001
            print("[scheduler] El refresco falló:")
            traceback.print_exc()
        # Evita re-disparar dentro del mismo minuto.
        time.sleep(61)


if __name__ == "__main__":
    main()
