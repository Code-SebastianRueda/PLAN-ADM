"""
azure_costs.py
=============
Azure Cost Management (apartado 1.2) — DATOS REALES.

Centro de control de costos: cuánto se gasta, qué cambió, dónde está el gasto,
qué recursos lo generan, dónde ahorrar y qué acción tomar.

Fuentes Azure:
  Azure Cost Management (Microsoft.CostManagement/query, POST)
    - Costo diario (Daily)                       -> tendencia, acumulado, variación.
    - Costo por servicio (ServiceName)           -> ranking de servicios.
    - Costo por resource group (ResourceGroupName)
    - Costo por recurso (ResourceId)             -> recursos de mayor costo.
    - Mismas queries sobre el periodo ANTERIOR   -> variación por dimensión.
  Azure Consumption (Microsoft.Consumption/budgets)
    - Presupuestos configurados                  -> % usado, saldo, riesgo.
  Azure Advisor (recommendations, category = Cost)
    - Oportunidades de ahorro                    -> dónde ahorrar / quick wins.

Multi-suscripción EN PARALELO. Reutiliza _arm_get/_arm_post de azure_alerts
(cliente ARM cacheado). Nunca inventa datos.
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
_BUDGET_API = "2023-05-01"
_ADVISOR_API = "2023-01-01"
_MAX_WORKERS = 12

_IMPACT_LABEL = {"high": "Alto", "medium": "Medio", "low": "Bajo"}
_IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}

# Palabras clave -> categoria y esfuerzo de la recomendacion de ahorro.
_SAVING_RULES = [
    (("unattached", "unused disk", "disco", "orphan"), "Recursos sin uso", "Bajo"),
    (("right-size", "rightsiz", "resize", "sobredimension", "underutil",
      "idle"), "Right-sizing", "Medio"),
    (("public ip", "ip address", "ip pública", "ip publica"),
     "Networking", "Bajo"),
    (("reserved", "reservation", "savings plan"), "Reservations", "Medio"),
    (("sql",), "SQL", "Medio"),
    (("storage", "blob", "snapshot"), "Storage", "Bajo"),
]


def _categorize_saving(text: str) -> tuple:
    t = (text or "").lower()
    for kws, cat, effort in _SAVING_RULES:
        if any(k in t for k in kws):
            return cat, effort
    return "Optimización", "Medio"


# ---------------------------------------------------------------------------
# Cost Management query
# ---------------------------------------------------------------------------
def _cost_query(credential, sub_id, grouping=None, days=30,
                end: Optional[dt.date] = None, start: Optional[dt.date] = None):
    """POST a Cost Management. Devuelve (columns, rows). grouping: dimension o None."""
    scope = f"/subscriptions/{sub_id}"
    url = (f"https://management.azure.com{scope}/providers/"
           f"Microsoft.CostManagement/query?api-version={_CM_API}")
    end = end or dt.date.today()
    start = start or (end - dt.timedelta(days=days))
    dataset = {"granularity": "None" if grouping else "Daily",
               "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}}
    if grouping:
        dataset["grouping"] = [{"type": "Dimension", "name": grouping}]
    body = {"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {
                "from": dt.datetime.combine(start, dt.time.min).isoformat() + "Z",
                "to": dt.datetime.combine(end, dt.time.max).isoformat() + "Z"},
            "dataset": dataset}
    payload = _az._arm_post(credential, sub_id, url, body)
    props = payload.get("properties", {}) or {}
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = props.get("rows", []) or []
    return cols, rows


def _rows_to_records(cols, rows, dim_name, dim_out):
    idx = {name: i for i, name in enumerate(cols)}
    out = []
    for r in rows:
        cost = r[idx.get("Cost", 0)] if "Cost" in idx else r[0]
        rec = {dim_out: (r[idx[dim_name]] if dim_name in idx else ""),
               "amount": round(float(cost or 0), 2),
               "currency": r[idx["Currency"]] if "Currency" in idx else ""}
        out.append(rec)
    return out


def _grouped_cost(credential, sub_id, sub_name, grouping, dim_out, days):
    """Costo agrupado por una dimension, periodo actual y anterior."""
    today = dt.date.today()
    cur_start = today - dt.timedelta(days=days)
    prev_start = today - dt.timedelta(days=days * 2)
    prev_end = cur_start - dt.timedelta(days=1)
    try:
        c, rows = _cost_query(credential, sub_id, grouping, days)
        cur = _rows_to_records(c, rows, grouping, dim_out)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(), None
    prev_map = {}
    try:
        c2, rows2 = _cost_query(credential, sub_id, grouping, days,
                                end=prev_end, start=prev_start)
        for rec in _rows_to_records(c2, rows2, grouping, dim_out):
            prev_map[rec[dim_out]] = rec["amount"]
    except Exception:  # noqa: BLE001
        pass
    for rec in cur:
        rec["prev"] = prev_map.get(rec[dim_out], 0.0)
        rec["subscriptionName"] = sub_name
    return pd.DataFrame(cur), None


def _daily_cost(credential, sub_id, sub_name, days):
    cols = ["date", "amount", "subscriptionName"]
    try:
        c, rows = _cost_query(credential, sub_id, None, days)
        idx = {name: i for i, name in enumerate(c)}
        out = []
        for r in rows:
            cost = r[idx.get("Cost", 0)] if "Cost" in idx else r[0]
            d = str(r[idx["UsageDate"]]) if "UsageDate" in idx else ""
            iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
            out.append({"date": iso, "amount": round(float(cost or 0), 2),
                        "subscriptionName": sub_name})
        return pd.DataFrame(out, columns=cols), None
    except HttpResponseError as exc:
        code = getattr(exc, "status_code", None)
        if code in (401, 403):
            return pd.DataFrame(columns=cols), "Sin permisos para Cost Management."
        return pd.DataFrame(columns=cols), f"Cost Management: HTTP {code}"
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame(columns=cols), f"{type(exc).__name__}"


def _resource_cost(credential, sub_id, sub_name, days, top=40):
    """Costo por recurso (ResourceId) actual y anterior, top N."""
    try:
        c, rows = _cost_query(credential, sub_id, "ResourceId", days)
        cur = _rows_to_records(c, rows, "ResourceId", "resourceId")
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    today = dt.date.today()
    prev_start = today - dt.timedelta(days=days * 2)
    prev_end = today - dt.timedelta(days=days) - dt.timedelta(days=1)
    prev_map = {}
    try:
        c2, rows2 = _cost_query(credential, sub_id, "ResourceId", days,
                                end=prev_end, start=prev_start)
        for rec in _rows_to_records(c2, rows2, "ResourceId", "resourceId"):
            prev_map[rec["resourceId"]] = rec["amount"]
    except Exception:  # noqa: BLE001
        pass
    for rec in cur:
        rid = rec["resourceId"]
        rec["prev"] = prev_map.get(rid, 0.0)
        rec["resource"] = _az._resource_name_from_id(rid)
        rec["resourceType"] = _az._resource_type_pretty(_type_from_id(rid))
        rec["resourceGroup"] = _az._rg_from_id(rid)
        rec["subscriptionName"] = sub_name
    df = pd.DataFrame(cur)
    if not df.empty:
        df = df.sort_values("amount", ascending=False).head(top)
    return df


def _type_from_id(res_id: str) -> str:
    if not res_id or "/providers/" not in res_id:
        return ""
    tail = res_id.split("/providers/")[-1].split("/")
    return f"{tail[0]}/{tail[1]}" if len(tail) >= 2 else ""


def _budgets(credential, sub_id, sub_name):
    url = (f"/subscriptions/{sub_id}/providers/Microsoft.Consumption/budgets"
           f"?api-version={_BUDGET_API}")
    out = []
    try:
        payload = _az._arm_get(credential, sub_id, url)
        for b in payload.get("value", []):
            p = b.get("properties", {}) or {}
            cur = (p.get("currentSpend", {}) or {}).get("amount")
            out.append({"name": b.get("name", ""), "amount": p.get("amount"),
                        "current": cur, "subscriptionName": sub_name})
    except Exception:  # noqa: BLE001
        pass
    return out


def _advisor_cost(credential, sub_id, sub_name):
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
                savings = (ext.get("savingsAmount") or ext.get("annualSavingsAmount")
                           or ext.get("savings") or 0)
                try:
                    savings = float(savings)
                except Exception:  # noqa: BLE001
                    savings = 0.0
                # annualSavings -> mensual.
                if ext.get("annualSavingsAmount") and not ext.get("savingsAmount"):
                    savings = savings / 12
                res_id = (props.get("resourceMetadata", {}) or {}).get("resourceId", "")
                problem = short.get("problem", "") or "Optimización de costo"
                cat, effort = _categorize_saving(problem + " " +
                                                 (short.get("solution", "") or ""))
                out.append({
                    "name": problem,
                    "impact": _IMPACT_LABEL.get(impact, "Medio"),
                    "impactRank": _IMPACT_RANK.get(impact, 1),
                    "category": cat, "effort": effort,
                    "savings": round(savings, 2),
                    "solution": short.get("solution", "") or "",
                    "subscriptionName": sub_name,
                    "resourceId": res_id,
                })
            next_url = payload.get("nextLink") or ""
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Orquestacion
# ---------------------------------------------------------------------------
def load_costs(credential, subscriptions: List[dict], days: int = 30) -> dict:
    subs = [s for s in (subscriptions or [])
            if (s.get("subscriptionId") or s.get("id"))]
    base = {"daily": pd.DataFrame(), "services": pd.DataFrame(),
            "subs": pd.DataFrame(), "rgs": pd.DataFrame(),
            "rtypes": pd.DataFrame(),
            "resources": pd.DataFrame(), "advisor": pd.DataFrame(),
            "budgets": [], "errors": [], "cost_status": "error",
            "advisor_status": "error", "currency": "", "days": days,
            "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subscriptions_scanned": 0}
    if not subs or not _SDK_OK:
        return base

    name_by_id = {(s.get("subscriptionId") or s.get("id")):
                  (s.get("displayName") or s.get("name") or
                   (s.get("subscriptionId") or s.get("id"))) for s in subs}

    daily_f, svc_f, rg_f, rtype_f, res_f, adv_f = [], [], [], [], [], []
    budgets, errors = [], []
    cost_ok = advisor_ok = False

    def _work(sub):
        sid = sub.get("subscriptionId") or sub.get("id")
        name = sub.get("displayName") or sub.get("name") or sid
        d = {}
        d["daily"] = _daily_cost(credential, sid, name, days)
        d["svc"] = _grouped_cost(credential, sid, name, "ServiceName", "service", days)
        d["rg"] = _grouped_cost(credential, sid, name, "ResourceGroupName",
                                "resourceGroup", days)
        d["rtype"] = _grouped_cost(credential, sid, name, "ResourceType",
                                   "resourceType", days)
        d["res"] = _resource_cost(credential, sid, name, days)
        d["budgets"] = _budgets(credential, sid, name)
        d["advisor"] = _advisor_cost(credential, sid, name)
        d["name"] = name
        return d

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(subs))) as pool:
        for fut in as_completed([pool.submit(_work, s) for s in subs]):
            d = fut.result()
            dd, dnote = d["daily"]
            if dnote:
                errors.append(f"Costos · {d['name']}: {dnote}")
            else:
                cost_ok = True
                if not dd.empty:
                    daily_f.append(dd)
            sv, _ = d["svc"]
            if sv is not None and not sv.empty:
                svc_f.append(sv)
            rg, _ = d["rg"]
            if rg is not None and not rg.empty:
                rg_f.append(rg)
            rt, _ = d["rtype"]
            if rt is not None and not rt.empty:
                rtype_f.append(rt)
            if d["res"] is not None and not d["res"].empty:
                res_f.append(d["res"])
            if d["advisor"]:
                advisor_ok = True
                adv_f.append(pd.DataFrame(d["advisor"]))
            budgets += d["budgets"]

    def _concat(frames, cols=None):
        return (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame(columns=cols or []))

    daily = _concat(daily_f)
    currency = ""
    # Moneda desde cualquier query agrupada que la traiga.
    for f in svc_f:
        if "currency" in f.columns and f["currency"].astype(bool).any():
            currency = f["currency"][f["currency"].astype(bool)].iloc[0]
            break

    base.update({
        "daily": daily,
        "services": _concat(svc_f),
        "subs": _subs_cost_df(svc_f, name_by_id),  # derivado abajo
        "rgs": _concat(rg_f),
        "rtypes": _concat(rtype_f),
        "resources": _concat(res_f),
        "advisor": _concat(adv_f),
        "budgets": budgets,
        "errors": errors,
        "cost_status": "ok" if cost_ok else "error",
        "advisor_status": "ok" if advisor_ok else "error",
        "currency": currency,
        "subscriptions_scanned": len(subs),
    })
    return base


def _subs_cost_df(svc_frames, name_by_id):
    """Costo por suscripcion = suma de servicios por subscriptionName."""
    if not svc_frames:
        return pd.DataFrame(columns=["subscriptionName", "amount", "prev"])
    allsvc = pd.concat(svc_frames, ignore_index=True)
    if allsvc.empty or "subscriptionName" not in allsvc.columns:
        return pd.DataFrame(columns=["subscriptionName", "amount", "prev"])
    g = (allsvc.groupby("subscriptionName")
               .agg(amount=("amount", "sum"), prev=("prev", "sum"))
               .reset_index().sort_values("amount", ascending=False))
    return g


# ---------------------------------------------------------------------------
# Derivados / calculos
# ---------------------------------------------------------------------------
def _variation(cur: float, prev: float) -> Optional[float]:
    if prev and prev > 0:
        return round((cur - prev) / prev * 100, 1)
    return None


def kpis(data: dict) -> dict:
    daily = data.get("daily", pd.DataFrame())
    advisor = data.get("advisor", pd.DataFrame())
    budgets = data.get("budgets", [])
    out = {"acumulado": 0.0, "proyeccion": 0.0, "variacion_pct": None,
           "ahorro": 0.0, "optimizaciones": 0, "presupuesto": None,
           "presupuesto_pct": None, "disponible": None,
           "currency": data.get("currency", "")}
    if daily is not None and not daily.empty:
        g = daily.groupby("date", as_index=False)["amount"].sum().sort_values("date")
        total = float(g["amount"].sum())
        out["acumulado"] = round(total, 2)
        n = max(1, len(g))
        out["proyeccion"] = round(total / n * 30, 2)
        if n >= 4:
            half = n // 2
            prev = g["amount"].iloc[:half].sum()
            curr = g["amount"].iloc[half:].sum()
            out["variacion_pct"] = _variation(curr, prev)
    if advisor is not None and not advisor.empty:
        out["ahorro"] = round(float(advisor["savings"].sum()), 2)
        out["optimizaciones"] = int(advisor["resourceId"].replace("", pd.NA).nunique())
    # Presupuesto: suma de budgets configurados vs consumo actual.
    if budgets:
        tot_budget = sum(float(b.get("amount") or 0) for b in budgets)
        tot_spent = sum(float(b.get("current") or 0) for b in budgets)
        if tot_budget > 0:
            out["presupuesto"] = round(tot_budget, 2)
            out["presupuesto_pct"] = round(tot_spent / tot_budget * 100, 1)
            out["disponible"] = round(tot_budget - tot_spent, 2)
    return out


def services_ranking(data: dict, top: int = 6) -> pd.DataFrame:
    cols = ["service", "amount", "prev", "pct", "variacion"]
    svc = data.get("services", pd.DataFrame())
    if svc is None or svc.empty:
        return pd.DataFrame(columns=cols)
    g = (svc.groupby("service").agg(amount=("amount", "sum"),
                                    prev=("prev", "sum")).reset_index())
    total = g["amount"].sum()
    g["pct"] = (g["amount"] / total * 100).round(1) if total else 0
    g["variacion"] = g.apply(lambda r: _variation(r["amount"], r["prev"]), axis=1)
    g = g.sort_values("amount", ascending=False).reset_index(drop=True)
    if len(g) > top:
        head = g.head(top - 1).copy()
        otros = pd.DataFrame([{"service": "Otros",
                               "amount": round(g["amount"][top-1:].sum(), 2),
                               "prev": round(g["prev"][top-1:].sum(), 2),
                               "pct": round(g["pct"][top-1:].sum(), 1),
                               "variacion": None}])
        g = pd.concat([head, otros], ignore_index=True)
    return g[cols]


def subs_ranking(data: dict) -> pd.DataFrame:
    cols = ["subscriptionName", "amount", "pct", "variacion"]
    s = data.get("subs", pd.DataFrame())
    if s is None or s.empty:
        return pd.DataFrame(columns=cols)
    total = s["amount"].sum()
    s = s.copy()
    s["pct"] = (s["amount"] / total * 100).round(1) if total else 0
    s["variacion"] = s.apply(lambda r: _variation(r["amount"], r.get("prev", 0)), axis=1)
    return s.sort_values("amount", ascending=False).reset_index(drop=True)[cols]


def rg_ranking(data: dict, top: int = 15) -> pd.DataFrame:
    cols = ["resourceGroup", "subscriptionName", "amount", "prev", "variacion", "pct"]
    rg = data.get("rgs", pd.DataFrame())
    if rg is None or rg.empty:
        return pd.DataFrame(columns=cols)
    g = (rg.groupby(["resourceGroup", "subscriptionName"])
           .agg(amount=("amount", "sum"), prev=("prev", "sum")).reset_index())
    total = g["amount"].sum()
    g["pct"] = (g["amount"] / total * 100).round(1) if total else 0
    g["variacion"] = g.apply(lambda r: _variation(r["amount"], r["prev"]), axis=1)
    return g.sort_values("amount", ascending=False).head(top).reset_index(drop=True)[cols]


def resource_type_ranking(data: dict, top: int = 10) -> pd.DataFrame:
    """Costo por TIPO de recurso (prettificado), con variación y % del total."""
    cols = ["resourceType", "amount", "prev", "variacion", "pct"]
    rt = data.get("rtypes", pd.DataFrame())
    if rt is None or rt.empty:
        return pd.DataFrame(columns=cols)
    g = rt.copy()
    g["resourceType"] = g["resourceType"].map(
        lambda t: _az._resource_type_pretty(t) if t else "Otros")
    g = (g.groupby("resourceType").agg(amount=("amount", "sum"),
                                       prev=("prev", "sum")).reset_index())
    total = g["amount"].sum()
    g["pct"] = (g["amount"] / total * 100).round(1) if total else 0
    g["variacion"] = g.apply(lambda r: _variation(r["amount"], r["prev"]), axis=1)
    g = g.sort_values("amount", ascending=False).reset_index(drop=True)
    if len(g) > top:
        head = g.head(top - 1).copy()
        otros = pd.DataFrame([{"resourceType": "Otros",
                               "amount": round(g["amount"][top-1:].sum(), 2),
                               "prev": round(g["prev"][top-1:].sum(), 2),
                               "variacion": None,
                               "pct": round(g["pct"][top-1:].sum(), 1)}])
        g = pd.concat([head, otros], ignore_index=True)
    return g[cols]


def top_resources(data: dict, top: int = 20) -> pd.DataFrame:
    cols = ["resource", "resourceType", "resourceGroup", "subscriptionName",
            "amount", "prev", "variacion", "pct", "resourceId"]
    r = data.get("resources", pd.DataFrame())
    if r is None or r.empty:
        return pd.DataFrame(columns=cols)
    g = r.copy()
    total = g["amount"].sum()
    g["pct"] = (g["amount"] / total * 100).round(1) if total else 0
    g["variacion"] = g.apply(lambda x: _variation(x["amount"], x.get("prev", 0)), axis=1)
    return g.sort_values("amount", ascending=False).head(top).reset_index(drop=True)[
        [c for c in cols if c in g.columns]]


def anomalies(data: dict, threshold: float = 20.0, top: int = 10) -> pd.DataFrame:
    """Recursos con incremento > threshold% frente al periodo anterior."""
    cols = ["resource", "resourceType", "subscriptionName", "amount",
            "prev", "variacion"]
    r = data.get("resources", pd.DataFrame())
    if r is None or r.empty:
        return pd.DataFrame(columns=cols)
    g = r.copy()
    g["variacion"] = g.apply(lambda x: _variation(x["amount"], x.get("prev", 0)), axis=1)
    g = g[(g["variacion"].notna()) & (g["variacion"] >= threshold)
          & (g["amount"] > 0)]
    return g.sort_values("variacion", ascending=False).head(top).reset_index(drop=True)[
        [c for c in cols if c in g.columns]]


def savings_opportunities(data: dict, top: int = 8) -> pd.DataFrame:
    cols = ["name", "category", "impact", "impactRank", "effort", "recursos",
            "savings", "solution"]
    adv = data.get("advisor", pd.DataFrame())
    if adv is None or adv.empty:
        return pd.DataFrame(columns=cols)
    g = (adv.groupby("name").agg(
            category=("category", "first"), impact=("impact", "first"),
            impactRank=("impactRank", "min"), effort=("effort", "first"),
            recursos=("resourceId", lambda s: s.replace("", pd.NA).nunique()),
            savings=("savings", "sum"), solution=("solution", "first"))
         .reset_index())
    g = g.sort_values(["savings", "impactRank"],
                      ascending=[False, True]).head(top).reset_index(drop=True)
    return g[cols]


def quick_wins(data: dict, top: int = 5) -> pd.DataFrame:
    """Oportunidades de bajo esfuerzo (quick wins)."""
    sav = savings_opportunities(data, top=100)
    if sav.empty:
        return sav
    qw = sav[sav["effort"] == "Bajo"].sort_values("savings", ascending=False)
    return qw.head(top).reset_index(drop=True)


def executive_summary(data: dict) -> str:
    """Resumen ejecutivo dinamico a partir de los datos reales."""
    k = kpis(data)
    cur = k.get("currency", "")
    svc = services_ranking(data, top=3)
    partes = []
    if k["variacion_pct"] is not None:
        signo = "aumentó" if k["variacion_pct"] >= 0 else "disminuyó"
        partes.append(f"El consumo {signo} {abs(k['variacion_pct'])}% frente al "
                      "periodo anterior.")
    if not svc.empty:
        top_svc = svc.iloc[0]["service"]
        partes.append(f"{top_svc} representa el mayor componente del gasto.")
    if k["optimizaciones"]:
        partes.append(f"Existen {k['optimizaciones']} recursos con oportunidades "
                      "de optimización.")
    if k["ahorro"]:
        partes.append(f"El ahorro potencial estimado es de "
                      f"{_fmt_money(k['ahorro'], cur)} mensuales.")
    if not partes:
        return "No hay datos suficientes de costo en el alcance seleccionado."
    return " ".join(partes)


def _fmt_money(v, cur=""):
    try:
        v = float(v or 0)
    except Exception:  # noqa: BLE001
        v = 0.0
    if abs(v) >= 1_000_000:
        s = f"${v/1_000_000:.1f}M"
    elif abs(v) >= 1_000:
        s = f"${v/1_000:.1f}K"
    else:
        s = f"${v:.0f}"
    return s.replace(".", ",") + (f" {cur}" if cur else "")


def daily_trend(data: dict, last_days: int = 30) -> pd.DataFrame:
    cols = ["date", "amount"]
    daily = data.get("daily", pd.DataFrame())
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    g = daily.groupby("date", as_index=False)["amount"].sum().sort_values("date")
    return g.tail(last_days).reset_index(drop=True)[cols]


def budget_detail(data: dict) -> dict:
    """Detalle de presupuesto agregado + estado y días restantes del mes."""
    budgets = data.get("budgets", [])
    out = {"has_budget": False, "configurado": 0.0, "consumido": 0.0,
           "pct": None, "disponible": 0.0, "proyeccion": None,
           "dias_restantes": 0, "estado": "Normal", "excede": False,
           "n_budgets": 0}
    k = kpis(data)
    out["proyeccion"] = k.get("proyeccion")
    # Dias restantes del mes.
    today = dt.date.today()
    if today.month == 12:
        fin = dt.date(today.year + 1, 1, 1)
    else:
        fin = dt.date(today.year, today.month + 1, 1)
    out["dias_restantes"] = (fin - today).days
    if budgets:
        conf = sum(float(b.get("amount") or 0) for b in budgets)
        cons = sum(float(b.get("current") or 0) for b in budgets)
        out["n_budgets"] = len(budgets)
        if conf > 0:
            out["has_budget"] = True
            out["configurado"] = round(conf, 2)
            out["consumido"] = round(cons, 2)
            out["pct"] = round(cons / conf * 100, 1)
            out["disponible"] = round(conf - cons, 2)
            proy = out["proyeccion"] or 0
            out["excede"] = proy > conf
            if out["pct"] >= 95 or out["excede"]:
                out["estado"] = "Riesgo"
            elif out["pct"] >= 85:
                out["estado"] = "Atención"
            else:
                out["estado"] = "Normal"
    return out


def new_resources_impact(data: dict, top: int = 8) -> pd.DataFrame:
    """Recursos cuyo costo del periodo anterior era ~0 y ahora tienen gasto
    relevante (proxy de 'nuevos con impacto')."""
    cols = ["resource", "resourceType", "subscriptionName", "amount", "resourceId"]
    r = data.get("resources", pd.DataFrame())
    if r is None or r.empty:
        return pd.DataFrame(columns=cols)
    g = r.copy()
    nuevos = g[(g["prev"].fillna(0) <= 0.5) & (g["amount"] > 0)]
    nuevos = nuevos.sort_values("amount", ascending=False).head(top)
    return nuevos.reset_index(drop=True)[[c for c in cols if c in nuevos.columns]]
