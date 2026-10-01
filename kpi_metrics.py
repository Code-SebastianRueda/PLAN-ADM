"""
kpi_metrics.py
==============
Cálculo y estructuración de KPIs operativos del PDM.

Métricas implementadas (con metas y semáforo Rojo/Amarillo/Verde):

    MTTA          Tiempo medio de reconocimiento          ≤ 15 min
    MTTR          Tiempo medio de resolución              ≤ 120 min
    FP_PCT        % Falsos positivos                      ≤ 10 %
    PROACTIVE_PCT % Detección proactiva                   ≥ 80 %
    ESC_OK_PCT    % Escalamientos sin error               ≥ 98 %
    CHG_OK_PCT    % Cumplimiento de cambios               ≥ 95 %
    COST_RED_PCT  % Reducción de costos                   ≥ 10 %
    AUTO_PCT      % Automatizaciones completadas          ≥ 90 %
    PROJ_OK_PCT   % Proyectos sin incidentes post-impl    ≥ 95 %
    PERF_PCT      % Mejora de rendimiento                 ≥ 15 %

Cada KPI se calcula, se compara con su meta y se devuelve con:
    - value         : float | None
    - target        : float
    - status        : "ok" | "warn" | "fail" | "nd"
    - color         : "green" | "orange" | "red" | "gray"
    - emoji         : 🟢 | 🟡 | 🔴 | ⚪
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class KPI:
    key: str
    label: str
    value: Optional[float]
    unit: str
    target: float
    direction: str           # "lte" (menor=mejor) | "gte" (mayor=mejor)
    status: str = "nd"
    color: str = "gray"
    emoji: str = "⚪"
    description: str = ""

    def to_dict(self) -> Dict:
        return {
            "KPI": self.label,
            "Valor": f"{self.value} {self.unit}" if self.value is not None else "—",
            "Meta": f"{'≤' if self.direction == 'lte' else '≥'} {self.target} {self.unit}",
            "Estado": f"{self.emoji} {self.status.upper()}",
        }


_EMOJI = {"green": "🟢", "orange": "🟡", "red": "🔴", "gray": "⚪"}
_TOL = 0.10   # ±10 % de margen para "warn"


def _eval(value: Optional[float], target: float, direction: str) -> (str, str):
    if value is None:
        return "nd", "gray"
    if direction == "lte":
        ok = value <= target
        warn = value <= target * (1 + _TOL)
    else:
        ok = value >= target
        warn = value >= target * (1 - _TOL)
    if ok:
        return "ok", "green"
    if warn:
        return "warn", "orange"
    return "fail", "red"


def _kpi(key, label, value, unit, target, direction, desc="") -> KPI:
    v = round(value, 2) if value is not None else None
    status, color = _eval(v, target, direction)
    return KPI(key, label, v, unit, target, direction, status, color,
               _EMOJI[color], desc)


def _safe_div(num, den):
    return (num / den) if den else None


# ── Cálculos públicos ─────────────────────────────────────────────────────────
def calc_mtta(sum_ack_min: float, ack_count: int) -> Optional[float]:
    return _safe_div(sum_ack_min, ack_count)


def calc_mttr(sum_res_min: float, resolved_count: int) -> Optional[float]:
    return _safe_div(sum_res_min, resolved_count)


def calc_fp_pct(fp_count: int, total_alerts: int) -> Optional[float]:
    v = _safe_div(fp_count * 100, total_alerts)
    return v


def calc_proactive_pct(proactive: int, total_alerts: int) -> Optional[float]:
    return _safe_div(proactive * 100, total_alerts)


def calc_esc_ok_pct(ok: int, total: int) -> Optional[float]:
    return _safe_div(ok * 100, total)


def calc_chg_ok_pct(compliant: int, total: int) -> Optional[float]:
    return _safe_div(compliant * 100, total)


def calc_cost_reduction(baseline: float, current: float) -> Optional[float]:
    return _safe_div((baseline - current) * 100, baseline)


def calc_auto_pct(done: int, planned: int) -> Optional[float]:
    return _safe_div(done * 100, planned)


def calc_proj_ok_pct(no_incident: int, total: int) -> Optional[float]:
    return _safe_div(no_incident * 100, total)


def calc_perf_pct(before: float, after: float) -> Optional[float]:
    return _safe_div((after - before) * 100, before)


# ── Generación de listas de KPIs ─────────────────────────────────────────────
def build_kpis(data: Dict) -> List[KPI]:
    """
    Recibe un dict con las métricas brutas y devuelve la lista completa de KPIs.

    Claves esperadas (todas opcionales — valor None si no se proveen):
        sum_ack_min, ack_count, sum_res_min, resolved_count,
        fp_count, total_alerts, proactive_alerts,
        esc_ok, esc_total, chg_ok, chg_total,
        cost_baseline, cost_current,
        auto_done, auto_planned,
        proj_ok, proj_total,
        perf_before, perf_after
    """
    g = data.get

    return [
        _kpi("mtta", "MTTA (reconocimiento)",
             calc_mtta(g("sum_ack_min", 0), g("ack_count", 0)),
             "min", 15, "lte",
             "Tiempo medio desde alerta hasta reconocimiento."),

        _kpi("mttr", "MTTR (resolución)",
             calc_mttr(g("sum_res_min", 0), g("resolved_count", 0)),
             "min", 120, "lte",
             "Tiempo medio desde alerta hasta resolución."),

        _kpi("fp_pct", "% Falsos Positivos",
             calc_fp_pct(g("fp_count", 0), g("total_alerts", 0)),
             "%", 10, "lte",
             "Alertas que resultaron no accionables."),

        _kpi("proactive_pct", "% Detección Proactiva",
             calc_proactive_pct(g("proactive_alerts", 0), g("total_alerts", 0)),
             "%", 80, "gte",
             "Alertas detectadas antes del impacto al usuario."),

        _kpi("esc_ok_pct", "% Escalamientos sin error",
             calc_esc_ok_pct(g("esc_ok", 0), g("esc_total", 0)),
             "%", 98, "gte",
             "Escalamientos gestionados correctamente."),

        _kpi("chg_ok_pct", "% Cumplimiento de cambios",
             calc_chg_ok_pct(g("chg_ok", 0), g("chg_total", 0)),
             "%", 95, "gte",
             "Cambios ejecutados dentro de política/ventana."),

        _kpi("cost_red", "% Reducción de costos",
             calc_cost_reduction(g("cost_baseline", 0), g("cost_current", 0)),
             "%", 10, "gte",
             "Ahorro aplicado respecto a línea base."),

        _kpi("auto_pct", "% Automatizaciones completadas",
             calc_auto_pct(g("auto_done", 0), g("auto_planned", 0)),
             "%", 90, "gte",
             "Automatizaciones entregadas vs planificadas."),

        _kpi("proj_ok_pct", "% Proyectos sin incidentes",
             calc_proj_ok_pct(g("proj_ok", 0), g("proj_total", 0)),
             "%", 95, "gte",
             "Proyectos sin incidentes post-implementación."),

        _kpi("perf_pct", "% Mejora de rendimiento",
             calc_perf_pct(g("perf_before", 0), g("perf_after", 0)),
             "%", 15, "gte",
             "Ganancia de rendimiento tras optimización."),
    ]


def summary_status(kpis: List[KPI]) -> str:
    """Devuelve el estado global del período: OK / WARN / FAIL."""
    if any(k.status == "fail" for k in kpis):
        return "🔴 FAIL"
    if any(k.status in ("warn", "nd") for k in kpis):
        return "🟡 WARN"
    return "🟢 OK"
