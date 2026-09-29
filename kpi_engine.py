"""
kpi_engine.py
=============
Motor de cálculo de KPIs del Plan de Administración Operativa Cloud (PDM).

Implementa las fórmulas de las matrices operativas por nivel:

    - L1 (Operación y Monitoreo): MTTA, MTTR, % Falsos Positivos,
      % Proactividad.
    - L2 (Soporte Avanzado): % Escalamientos sin error, % Cumplimiento de
      cambios, % Reducción de costos.
    - L3 (Arquitectura/Mejora): % Automatizaciones completadas, % Proyectos
      sin incidentes, % Mejora de rendimiento.

Cada KPI se evalúa contra una META y devuelve un estado con color:
    - 🟢 verde  -> cumple
    - 🟡 ámbar  -> cerca del objetivo (dentro del margen de tolerancia)
    - 🔴 rojo   -> incumple

El módulo NO depende de Streamlit: sólo cálculo puro para poder testear y
reutilizar. La UI consume estos resultados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ===========================================================================
# Estructuras
# ===========================================================================
@dataclass
class KPIResult:
    key: str
    label: str
    value: Optional[float]
    unit: str
    target: float
    goal_direction: str          # "lte" (menor es mejor) o "gte" (mayor es mejor)
    status: str = "n/d"          # "ok" | "warn" | "fail" | "n/d"
    color: str = "gray"          # "green" | "orange" | "red" | "gray"
    detail: str = ""

    def as_dict(self) -> Dict:
        return {
            "kpi": self.label,
            "valor": self.value,
            "unidad": self.unit,
            "meta": self.target,
            "estado": self.status,
        }


# ===========================================================================
# Evaluación genérica contra meta
# ===========================================================================
def _evaluate(value: Optional[float], target: float, direction: str,
              tol: float = 0.1) -> (str, str):
    """
    Devuelve (status, color).
    direction "lte": cumple si value <= target (menor es mejor).
    direction "gte": cumple si value >= target (mayor es mejor).
    tol: margen relativo para el estado 'warn' (ámbar).
    """
    if value is None:
        return "n/d", "gray"

    if direction == "lte":
        if value <= target:
            return "ok", "green"
        if value <= target * (1 + tol):
            return "warn", "orange"
        return "fail", "red"
    else:  # gte
        if value >= target:
            return "ok", "green"
        if value >= target * (1 - tol):
            return "warn", "orange"
        return "fail", "red"


def _mk(key, label, value, unit, target, direction, detail="") -> KPIResult:
    status, color = _evaluate(value, target, direction)
    return KPIResult(key, label, value, unit, target, direction, status, color, detail)


# ===========================================================================
# NIVEL 1 — Operación y Monitoreo Proactivo
# ===========================================================================
def kpis_l1(*, alerts_total: int = 0, alerts_ack: int = 0,
            sum_ack_minutes: float = 0.0, incidents_resolved: int = 0,
            sum_resolution_minutes: float = 0.0, false_positives: int = 0,
            proactive_alerts: int = 0) -> List[KPIResult]:
    """
    Fórmulas L1:
        MTTA (min)        = Σ tiempo de reconocimiento / nº alertas reconocidas   (meta ≤ 15)
        MTTR (min)        = Σ tiempo de resolución / nº incidentes resueltos       (meta ≤ 120)
        % Falsos Positivos= falsos positivos / total alertas * 100                 (meta ≤ 10)
        % Proactividad    = alertas proactivas / total alertas * 100               (meta ≥ 80)
    """
    mtta = (sum_ack_minutes / alerts_ack) if alerts_ack else None
    mttr = (sum_resolution_minutes / incidents_resolved) if incidents_resolved else None
    fp_pct = (false_positives / alerts_total * 100) if alerts_total else None
    proactive_pct = (proactive_alerts / alerts_total * 100) if alerts_total else None

    return [
        _mk("mtta", "MTTA (reconocimiento)", _round(mtta), "min", 15, "lte",
            "Tiempo medio en reconocer una alerta."),
        _mk("mttr", "MTTR (resolución)", _round(mttr), "min", 120, "lte",
            "Tiempo medio en resolver un incidente."),
        _mk("fp", "% Falsos Positivos", _round(fp_pct), "%", 10, "lte",
            "Alertas que resultaron no accionables."),
        _mk("proactive", "% Detección Proactiva", _round(proactive_pct), "%", 80, "gte",
            "Alertas detectadas antes del impacto al usuario."),
    ]


# ===========================================================================
# NIVEL 2 — Soporte Avanzado y Gestión Operativa
# ===========================================================================
def kpis_l2(*, escalations_total: int = 0, escalations_ok: int = 0,
            changes_total: int = 0, changes_compliant: int = 0,
            cost_baseline: float = 0.0, cost_current: float = 0.0) -> List[KPIResult]:
    """
    Fórmulas L2:
        % Escalamientos sin error = escalamientos ok / total * 100      (meta ≥ 98)
        % Cumplimiento de cambios = cambios conformes / total * 100     (meta ≥ 95)
        % Reducción de costos     = (baseline - actual)/baseline * 100  (meta ≥ 10)
    """
    esc_pct = (escalations_ok / escalations_total * 100) if escalations_total else None
    chg_pct = (changes_compliant / changes_total * 100) if changes_total else None
    cost_pct = ((cost_baseline - cost_current) / cost_baseline * 100) if cost_baseline else None

    return [
        _mk("esc_ok", "% Escalamientos sin error", _round(esc_pct), "%", 98, "gte",
            "Escalamientos gestionados correctamente."),
        _mk("chg_ok", "% Cumplimiento de cambios", _round(chg_pct), "%", 95, "gte",
            "Cambios ejecutados dentro de política/ventana."),
        _mk("cost_red", "% Reducción de costos", _round(cost_pct), "%", 10, "gte",
            "Ahorro aplicado respecto a la línea base."),
    ]


# ===========================================================================
# NIVEL 3 — Arquitectura, Automatización y Mejora Continua
# ===========================================================================
def kpis_l3(*, automations_planned: int = 0, automations_done: int = 0,
            projects_total: int = 0, projects_no_incident: int = 0,
            perf_before: float = 0.0, perf_after: float = 0.0) -> List[KPIResult]:
    """
    Fórmulas L3:
        % Automatizaciones completadas = hechas / planificadas * 100     (meta ≥ 90)
        % Proyectos sin incidentes     = sin incidente / total * 100     (meta ≥ 95)
        % Mejora de rendimiento        = (after - before)/before * 100   (meta ≥ 15)

    Nota: para perf, "mejora" asume que 'after' representa capacidad/throughput
    (mayor es mejor). Si midieras latencia, invierte el signo antes de pasar.
    """
    auto_pct = (automations_done / automations_planned * 100) if automations_planned else None
    proj_pct = (projects_no_incident / projects_total * 100) if projects_total else None
    perf_pct = ((perf_after - perf_before) / perf_before * 100) if perf_before else None

    return [
        _mk("auto", "% Automatizaciones completadas", _round(auto_pct), "%", 90, "gte",
            "Automatizaciones entregadas vs planificadas."),
        _mk("proj_ok", "% Proyectos sin incidentes", _round(proj_pct), "%", 95, "gte",
            "Proyectos sin incidentes post-implementación."),
        _mk("perf", "% Mejora de rendimiento", _round(perf_pct), "%", 15, "gte",
            "Ganancia de rendimiento tras optimización."),
    ]


# ===========================================================================
# Helpers
# ===========================================================================
def _round(value: Optional[float], ndigits: int = 2) -> Optional[float]:
    return round(value, ndigits) if value is not None else None


def results_to_records(results: List[KPIResult]) -> List[Dict]:
    return [r.as_dict() for r in results]


COLOR_EMOJI = {"green": "🟢", "orange": "🟡", "red": "🔴", "gray": "⚪"}


def status_badge(result: KPIResult) -> str:
    return f"{COLOR_EMOJI.get(result.color, '⚪')} {result.status.upper()}"
