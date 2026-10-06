"""
azure_costs.py
=============
Resumen de costos (apartado 1.2) — DATOS REALES.

Fuentes Azure:
  Azure Cost Management (Microsoft.CostManagement/query, POST)
    - Costo diario de los ultimos 30 dias  -> tendencia, acumulado, variacion.
    - Costo por servicio (grouping ServiceName) -> principales contribuyentes.
  Azure Advisor (recommendations, category = Cost)
    - Recomendaciones de ahorro -> 'donde ahorrar primero' y ahorro potencial.

Multi-suscripcion EN PARALELO. Reutiliza _arm_post/_arm_get del modulo de
alertas (cliente ARM cacheado). Nunca inventa datos: si Cost Management no
responde para una sub, se registra y el resto continua.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import pandas as pd

import azure_alerts as _az

try:
    from azure.core.exceptions import HttpResponseError
    _SDK_OK = True
except Exception:  # noqa: BLE001
    _SDK_OK = False


_CM_API = "2023-11-01"
_ADVISOR_API = "2025-01-01"
_MAX_WORKERS = 12

_IMPACT_LABEL = {"high": "Alto", "medium": "Medio", "low": "Bajo"}
_IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Cost Management: query de costo
# ---------------------------------------------------------------------------
def _cost_query(credential, sub_id, grouping_name=None, days=30):
    """POST a Cost Management. Devuelve (columns, rows) o lanza.

    grouping_name: 'ServiceName' para agrupar por servicio; None para diario.
    """
    scope = f"/subscriptions/{sub_id}"
    url = (f"https://management.azure.com{scope}/providers/"
           f"Microsoft.CostManagement/query?api-version={_CM_API}")
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    dataset = {
        "granularity": "None" if grouping_name else "Daily",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
    }
    if grouping_name:
        dataset["grouping"] = [{"type": "Dimension", "name": grouping_name}]
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": dt.datetime.combine(start, dt.time.min).isoformat() + "Z",
            "to": dt.datetime.combine(end, dt.time.max).isoformat() + "Z",
        },
        "dataset": dataset,
    }
    payload = _az._arm_post(credential, sub_id, url, body)
    props = payload.get("properties", {}) or {}
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = props.get("rows", []) or []
    return cols, rows


def _daily_cost_sub(credential, sub_id, sub_name, days=30):
    """Costo diario de una suscripcion. DataFrame [date, amount, currency]."""
    cols = ["date", "amount", "currency", "subscriptionName"]
    try:
        c, rows = _cost_query(credential, sub_id, None, days)
        idx = {name: i for i, name in enumerate(c)}
        out = []
        for r in rows:
            cost = r[idx.get("Cost", 0)] if "Cost" in idx else r[0]
            raw_date = r[idx["UsageDate"]] if "UsageDate" in idx else None
            cur = r[idx.get("Currency")] if "Currency" in idx else ""
            d = str(raw_date)
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
            out.append({"date": iso, "amount": round(float(cost or 0), 2),
                        "currency": cur, "subscriptionName": sub_name})
        return pd.DataFrame(out, columns=cols), None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return pd.DataFrame(columns=cols), "Sin permisos para Cost Management."
        if code == 404:
            return pd.DataFrame(columns=cols), "Cost Management no disponible."
        return pd.DataFrame(columns=cols), f"Error costos: {exc}"
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame(columns=cols), f"{type(exc).__name__}: {exc}"


def _service_cost_sub(credential, sub_id, sub_name, days=30):
    """Costo por servicio de una suscripcion. DataFrame [service, amount]."""
    cols = ["service", "amount", "currency", "subscriptionName"]
    try:
        c, rows = _cost_query(credential, sub_id, "ServiceName", days)
        idx = {name: i for i, name in enumerate(c)}
        out = []
        for r in rows:
            cost = r[idx.get("Cost", 0)] if "Cost" in idx else r[0]
            svc = r[idx["ServiceName"]] if "ServiceName" in idx else "Otros"
            cur = r[idx.get("Currency")] if "Currency" in idx else ""
            out.append({"service": svc or "Otros",
                        "amount": round(float(cost or 0), 2),
                        "currency": cur, "subscriptionName": sub_name})
        return pd.DataFrame(out, columns=cols), None
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=cols), None


def _advisor_cost_sub(credential, sub_id, sub_name):
    """Recomendaciones de ahorro (Advisor category=Cost) de una suscripcion."""
    cols = ["name", "impact", "impactRank", "recursos", "savings",
            "currency", "subscriptionName", "resourceId"]
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Advisor/recommendations"
           f"?api-version={_ADVISOR_API}")
    out = []
    try:
        next_url = url
        pages = 0
        while next_url and pages < 20:
            pages += 1
            payload = _az._arm_get(credential, sub_id, next_url)
            for rec in payload.get("value", []):
                props = rec.get("properties", {}) or {}
                if (props.get("category", "") or "").lower() != "cost":
                    continue
                short = props.get("shortDescription", {}) or {}
                impact = (props.get("impact", "") or "").lower()
                ext = props.get("extendedProperties", {}) or {}
                savings = ext.get("savingsAmount") or ext.get("annualSavingsAmount") \
                    or ext.get("savings") or 0
                try:
                    savings = float(savings)
                except Exception:  # noqa: BLE001
                    savings = 0.0
                res_id = (props.get("resourceMetadata", {}) or {}).get("resourceId", "")
                out.append({
                    "name": short.get("problem", "") or "Optimización de costo",
                    "impact": _IMPACT_LABEL.get(impact, "Medio"),
                    "impactRank": _IMPACT_RANK.get(impact, 1),
                    "recursos": 1,
                    "savings": round(savings, 2),
                    "currency": ext.get("savingsCurrency", "") or "",
                    "subscriptionName": sub_name,
                    "resourceId": res_id,
                })
            next_url = payload.get("nextLink") or ""
        return pd.DataFrame(out, columns=cols), None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return pd.DataFrame(columns=cols), "Sin permisos para Advisor (costos)."
        return pd.DataFrame(columns=cols), None
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=cols), None


# ---------------------------------------------------------------------------
# Orquestacion
# ---------------------------------------------------------------------------
def load_costs(credential, subscriptions: List[dict], days: int = 30) -> dict:
    """Carga costos (diario, por servicio) + recomendaciones de ahorro.

    Devuelve un dict con DataFrames y metadatos:
      {'daily', 'services', 'advisor', 'errors', 'cost_status',
       'advisor_status', 'currency', 'fetched_at', 'subscriptions_scanned'}
    """
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    daily_frames, svc_frames, adv_frames = [], [], []
    errors = []
    cost_ok = False
    advisor_ok = False

    if not subs or not _SDK_OK:
        return {"daily": pd.DataFrame(), "services": pd.DataFrame(),
                "advisor": pd.DataFrame(), "errors": [], "cost_status": "error",
                "advisor_status": "error", "currency": "",
                "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "subscriptions_scanned": 0}

    def _work(sub):
        sid = sub.get("subscriptionId") or sub.get("id")
        name = sub.get("displayName") or sub.get("name") or sid
        return {
            "daily": _daily_cost_sub(credential, sid, name, days),
            "services": _service_cost_sub(credential, sid, name, days),
            "advisor": _advisor_cost_sub(credential, sid, name),
        }

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(subs))) as pool:
        futures = [pool.submit(_work, s) for s in subs]
        for fut in as_completed(futures):
            r = fut.result()
            d_df, d_note = r["daily"]
            s_df, _ = r["services"]
            a_df, a_note = r["advisor"]
            if d_note:
                errors.append(f"Costos · {d_note}")
            else:
                cost_ok = True
                if not d_df.empty:
                    daily_frames.append(d_df)
            if not s_df.empty:
                svc_frames.append(s_df)
            if a_note:
                errors.append(f"Advisor · {a_note}")
            else:
                advisor_ok = True
                if not a_df.empty:
                    adv_frames.append(a_df)

    daily = pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame(
        columns=["date", "amount", "currency", "subscriptionName"])
    services = pd.concat(svc_frames, ignore_index=True) if svc_frames else pd.DataFrame(
        columns=["service", "amount", "currency", "subscriptionName"])
    advisor = pd.concat(adv_frames, ignore_index=True) if adv_frames else pd.DataFrame(
        columns=["name", "impact", "impactRank", "recursos", "savings",
                 "currency", "subscriptionName", "resourceId"])

    currency = ""
    if not daily.empty and daily["currency"].astype(bool).any():
        currency = daily["currency"][daily["currency"].astype(bool)].iloc[0]

    return {
        "daily": daily, "services": services, "advisor": advisor,
        "errors": errors,
        "cost_status": "ok" if cost_ok else "error",
        "advisor_status": "ok" if advisor_ok else "error",
        "currency": currency,
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subscriptions_scanned": len(subs),
    }


# ---------------------------------------------------------------------------
# Derivados
# ---------------------------------------------------------------------------
def daily_trend(daily: pd.DataFrame, last_days: int = 7) -> pd.DataFrame:
    """Costo diario agregado (suma de todas las subs) de los ultimos N dias."""
    cols = ["date", "amount"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    g = daily.groupby("date", as_index=False)["amount"].sum().sort_values("date")
    return g.tail(last_days).reset_index(drop=True)[cols]


def services_top(services: pd.DataFrame, top: int = 6) -> pd.DataFrame:
    """Top servicios por costo; agrupa el resto en 'Otros'."""
    cols = ["service", "amount"]
    if services is None or services.empty:
        return pd.DataFrame(columns=cols)
    g = (services.groupby("service", as_index=False)["amount"].sum()
                 .sort_values("amount", ascending=False).reset_index(drop=True))
    if len(g) > top:
        head = g.head(top - 1).copy()
        otros = pd.DataFrame([{"service": "Otros",
                               "amount": round(g["amount"][top - 1:].sum(), 2)}])
        g = pd.concat([head, otros], ignore_index=True)
    return g[cols]


def savings_opportunities(advisor: pd.DataFrame, top: int = 6) -> pd.DataFrame:
    """Agrupa recomendaciones de ahorro por nombre: recursos y ahorro total."""
    cols = ["name", "impact", "impactRank", "recursos", "savings"]
    if advisor is None or advisor.empty:
        return pd.DataFrame(columns=cols)
    g = (advisor.groupby("name")
                .agg(impact=("impact", "first"),
                     impactRank=("impactRank", "min"),
                     recursos=("resourceId", lambda s: s.replace("", pd.NA).nunique()),
                     savings=("savings", "sum"))
                .reset_index())
    g = g.sort_values(["savings", "impactRank"],
                      ascending=[False, True]).head(top).reset_index(drop=True)
    return g[cols]


def kpis(data: dict, budget: Optional[float] = None) -> dict:
    """KPIs de cabecera. 'budget' opcional para % presupuesto (si se conoce)."""
    daily = data.get("daily", pd.DataFrame())
    advisor = data.get("advisor", pd.DataFrame())
    out = {"acumulado": 0.0, "proyeccion": 0.0, "variacion_pct": None,
           "ahorro": 0.0, "optimizaciones": 0, "currency": data.get("currency", "")}
    if daily is None or daily.empty:
        if advisor is not None and not advisor.empty:
            out["ahorro"] = round(float(advisor["savings"].sum()), 2)
            out["optimizaciones"] = int(
                advisor["resourceId"].replace("", pd.NA).nunique())
        return out

    g = daily.groupby("date", as_index=False)["amount"].sum().sort_values("date")
    total = float(g["amount"].sum())
    out["acumulado"] = round(total, 2)
    # Proyeccion mensual: promedio diario * 30.
    n = max(1, len(g))
    out["proyeccion"] = round((total / n) * 30, 2)
    # Variacion: ultima mitad vs primera mitad del periodo.
    if n >= 4:
        half = n // 2
        prev = g["amount"].iloc[:half].sum()
        curr = g["amount"].iloc[half:].sum()
        if prev > 0:
            out["variacion_pct"] = round((curr - prev) / prev * 100, 1)
    if advisor is not None and not advisor.empty:
        out["ahorro"] = round(float(advisor["savings"].sum()), 2)
        out["optimizaciones"] = int(
            advisor["resourceId"].replace("", pd.NA).nunique())
    return out
