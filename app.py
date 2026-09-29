"""
app.py
======
Orquestador principal del Plan de Administración Operativa Cloud (PDM).

Fases:
  1. Autenticación y descubrimiento (AWS SSO + Azure InteractiveBrowser).
     La UI principal no se muestra hasta seleccionar un entorno válido.
  2. RBAC por Rol Operativo (L1/L2/L3) con módulos según la matriz operativa,
     organizados en pestañas Diario / Semanal / Mensual, más un panel de KPIs.

Rendimiento:
  Las consultas se ejecutan EN SEGUNDO PLANO (ThreadPoolExecutor) y se cachean
  en st.session_state para que la navegación entre pestañas sea fluida. Para
  Azure multi-suscripción, el barrido es paralelo (ver run_all_subscriptions).
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

import aws_services as aws
import azure_services as azure
import kpi_engine

try:
    import plotly.express as px
    import plotly.graph_objects as go
    _HAS_PLOTLY = True
except Exception:  # noqa: BLE001
    _HAS_PLOTLY = False


st.set_page_config(page_title="PDM Cloud Ops", page_icon="☁️", layout="wide")


# ===========================================================================
# Estado
# ===========================================================================
def init_state() -> None:
    defaults = {
        "aws_session": None, "aws_profile": None, "aws_identity": None,
        "aws_region": "us-east-1",
        "azure_credential": None, "azure_subs": None, "azure_scope": [],
        "authenticated": False,
        "cache": {},          # cache de resultados de consultas por clave
        "kpi_l1": {}, "kpi_l2": {}, "kpi_l3": {},  # entradas de KPIs por nivel
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_state()


# ===========================================================================
# Ejecución en segundo plano + cache
# ===========================================================================
def cached(key: str, fn, *args, **kwargs):
    """
    Ejecuta fn(*args, **kwargs) una sola vez por 'key' y cachea el resultado
    en session_state. Botones de 'refrescar' invalidan la clave.

    Importante: fn se ejecuta en el hilo principal (no en un hilo secundario)
    para poder acceder a st.session_state. El paralelismo real de las consultas
    Azure ocurre dentro de run_all_subscriptions (ThreadPoolExecutor propio).
    """
    cache = st.session_state["cache"]
    if key not in cache:
        with st.spinner("Consultando en segundo plano..."):
            cache[key] = fn(*args, **kwargs)
    return cache[key]


def invalidate(prefix: str = "") -> None:
    cache = st.session_state["cache"]
    for k in [k for k in cache if k.startswith(prefix)]:
        del cache[k]


# ===========================================================================
# Utilidades de UI
# ===========================================================================
def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            (df if df is not None else pd.DataFrame()).to_excel(
                writer, index=False, sheet_name="datos")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return b""


def export_buttons(df: pd.DataFrame, prefix: str) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    c1, c2 = st.columns(2)
    with c1:
        csv_buf = io.StringIO()
        (df if df is not None else pd.DataFrame()).to_csv(csv_buf, index=False)
        st.download_button("⬇️ CSV", csv_buf.getvalue().encode("utf-8-sig"),
                           f"{prefix}_{ts}.csv", "text/csv", key=f"csv_{prefix}_{ts}")
    with c2:
        xls = to_excel_bytes(df)
        if xls:
            st.download_button(
                "⬇️ Excel", xls, f"{prefix}_{ts}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"xls_{prefix}_{ts}")


def show_table(df: pd.DataFrame, prefix: str, empty_msg: str = "Sin resultados.") -> None:
    note = df.attrs.get("note") if hasattr(df, "attrs") else None
    scanned = df.attrs.get("subscriptions_scanned") if hasattr(df, "attrs") else None
    errors = df.attrs.get("errors") if hasattr(df, "attrs") else None

    if scanned is not None:
        st.caption(f"Suscripciones consultadas: {scanned}")
    if note:
        st.info(note)
    if df is None or df.empty:
        st.warning(empty_msg)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        if "subscriptionName" in df.columns and len(df) > 0:
            summary = (df.groupby("subscriptionName").size()
                         .reset_index(name="hallazgos")
                         .sort_values("hallazgos", ascending=False))
            with st.expander(f"Resumen por suscripción ({len(summary)} con hallazgos)"):
                st.dataframe(summary, use_container_width=True, hide_index=True)
        st.caption(f"Total de registros: {len(df)}")
    if errors:
        with st.expander(f"⚠️ {len(errors)} suscripción(es) con errores/permisos"):
            for e in errors:
                st.text(f"• {e}")
    export_buttons(df, prefix)


def cost_chart(df: pd.DataFrame, title: str) -> None:
    if df is None or df.empty or "service" not in df.columns:
        return
    agg = (df.groupby("service", as_index=False)["amount"].sum()
             .sort_values("amount", ascending=False).head(15))
    if _HAS_PLOTLY:
        fig = px.bar(agg, x="amount", y="service", orientation="h", title=title)
        fig.update_layout(yaxis=dict(autorange="reversed"), height=450)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.bar_chart(agg.set_index("service"))


def cost_by_sub_chart(df: pd.DataFrame) -> None:
    if df is None or df.empty or "subscriptionName" not in df.columns:
        return
    agg = (df.groupby("subscriptionName", as_index=False)["amount"].sum()
             .sort_values("amount", ascending=False))
    if _HAS_PLOTLY:
        fig = px.bar(agg, x="amount", y="subscriptionName", orientation="h",
                     title="Gasto por suscripción")
        fig.update_layout(yaxis=dict(autorange="reversed"), height=500)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.bar_chart(agg.set_index("subscriptionName"))


def auth_error(msg: str) -> None:
    st.error(f"🔒 **Sesión no válida o expirada.**\n\n{msg}\n\n➡️ Reautentícate y reintenta.")


# ===========================================================================
# Render de KPIs (con indicadores de color)
# ===========================================================================
_COLOR_HEX = {"green": "#1a9850", "orange": "#f0ad4e", "red": "#d73027", "gray": "#999999"}


def render_kpis(results, title: str) -> None:
    st.subheader(title)
    cols = st.columns(len(results)) if results else [st]
    for col, r in zip(cols, results):
        with col:
            emoji = kpi_engine.COLOR_EMOJI.get(r.color, "⚪")
            val = "—" if r.value is None else f"{r.value} {r.unit}"
            dir_txt = "≤" if r.goal_direction == "lte" else "≥"
            st.metric(label=f"{emoji} {r.label}", value=val,
                      delta=f"meta {dir_txt} {r.target} {r.unit}",
                      delta_color="off")
            st.caption(r.detail)

    # Gráfico comparativo valor vs meta
    if _HAS_PLOTLY and results:
        names = [r.label for r in results]
        values = [r.value if r.value is not None else 0 for r in results]
        targets = [r.target for r in results]
        colors = [_COLOR_HEX.get(r.color, "#999") for r in results]
        fig = go.Figure()
        fig.add_bar(x=names, y=values, name="Valor",
                    marker_color=colors)
        fig.add_scatter(x=names, y=targets, name="Meta", mode="markers",
                        marker=dict(symbol="line-ew", size=30, color="black"))
        fig.update_layout(height=380, title="Valor actual vs Meta")
        st.plotly_chart(fig, use_container_width=True)

    df = pd.DataFrame(kpi_engine.results_to_records(results))
    export_buttons(df, "kpis")


# ===========================================================================
# FASE 1 — Autenticación y Descubrimiento
# ===========================================================================
def render_login() -> None:
    st.title("🔐 Autenticación y Configuración")
    st.caption("Plan de Administración Operativa Cloud (PDM) — Fase 1")

    col_aws, col_azure = st.columns(2)

    # ---------------- AWS ----------------
    with col_aws:
        st.subheader("🟠 AWS — IAM Identity Center (SSO)")
        profiles_df = aws.describe_profiles()
        if profiles_df.empty:
            st.warning("No se detectaron perfiles en `~/.aws/config`. Usa `aws configure sso`.")
            manual = st.text_input("Perfil manual", key="aws_manual")
            profile_options = [manual] if manual else []
        else:
            with st.expander("Perfiles / cuentas en ~/.aws/config", expanded=True):
                st.dataframe(profiles_df, use_container_width=True, hide_index=True)
            profile_options = profiles_df["profile"].tolist()

        selected_profile = st.selectbox("Perfil SSO",
                                        profile_options or ["<sin perfiles>"],
                                        key="aws_profile_select")
        region = st.text_input("Región AWS", value=st.session_state["aws_region"],
                               key="aws_region_in")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("🌐 aws sso login", use_container_width=True):
                if selected_profile and selected_profile != "<sin perfiles>":
                    with st.spinner("Abriendo navegador para SSO..."):
                        res = aws.sso_login(selected_profile)
                    (st.success if res["ok"] else st.error)(res["message"])
                else:
                    st.warning("Selecciona un perfil válido.")
        with c2:
            if st.button("✅ Conectar AWS", type="primary", use_container_width=True):
                if selected_profile and selected_profile != "<sin perfiles>":
                    try:
                        with st.spinner(f"Conectando '{selected_profile}'..."):
                            session = aws.get_session(selected_profile, region_name=region)
                            identity = aws.whoami(session)
                        st.session_state.update({
                            "aws_session": session, "aws_profile": selected_profile,
                            "aws_identity": identity, "aws_region": region,
                            "authenticated": True,
                        })
                        st.success(f"Conectado a la cuenta {identity.get('Account')} ✅")
                    except aws.AWSAuthError as exc:
                        auth_error(str(exc))
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Error al conectar con AWS: {exc}")
                else:
                    st.warning("Selecciona un perfil válido.")

        if st.session_state["aws_identity"]:
            st.info(f"AWS conectado — Cuenta `{st.session_state['aws_identity'].get('Account')}`")

    # ---------------- Azure ----------------
    with col_azure:
        st.subheader("🔵 Azure — Login interactivo")
        auth_label = st.radio("Método",
                              ["Interactivo (navegador)", "az login (Default)", "Service Principal"],
                              key="azure_auth_method")
        mode = {"Interactivo (navegador)": "interactive",
                "az login (Default)": "default",
                "Service Principal": "sp"}[auth_label]

        tenant_id = None
        if mode in ("interactive", "default"):
            tenant_id = st.text_input(
                "Tenant ID (recomendado)", key="az_login_tenant",
                help="Si al conectar no aparecen suscripciones, especifica el "
                     "Tenant ID del directorio que las contiene.",
            ).strip() or None

        sp_tenant = sp_client = sp_secret = None
        if mode == "sp":
            sp_tenant = st.text_input("Tenant ID", key="az_tenant")
            sp_client = st.text_input("Client ID", key="az_client")
            sp_secret = st.text_input("Client Secret", type="password", key="az_secret")

        if st.button("✅ Conectar Azure y descubrir suscripciones",
                     type="primary", use_container_width=True):
            try:
                with st.spinner("Autenticando en Azure..."):
                    cred = azure.get_credential(
                        mode=mode, tenant_id=sp_tenant if mode == "sp" else tenant_id,
                        client_id=sp_client, client_secret=sp_secret)
                    subs = azure.discover_subscriptions(cred)
                if subs.empty:
                    st.warning("No se encontraron suscripciones con este login. "
                               "Revisa los tenants disponibles y reintenta con el correcto.")
                    try:
                        tenants = azure.discover_tenants(cred)
                        if not tenants.empty:
                            st.markdown("**Tenants disponibles:**")
                            st.dataframe(tenants, use_container_width=True, hide_index=True)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    st.session_state.update({
                        "azure_credential": cred, "azure_subs": subs,
                        "authenticated": True,
                    })
                    st.success(f"Azure conectado ✅ — {len(subs)} suscripción(es).")
            except azure.AzureAuthError as exc:
                auth_error(str(exc))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Error al conectar con Azure: {exc}")

        if st.session_state["azure_subs"] is not None and not st.session_state["azure_subs"].empty:
            st.dataframe(st.session_state["azure_subs"], use_container_width=True, hide_index=True)

    st.markdown("---")
    aws_ok = st.session_state["aws_session"] is not None
    azure_ok = (st.session_state["azure_subs"] is not None
                and not st.session_state["azure_subs"].empty)
    if aws_ok or azure_ok:
        st.success("Entorno válido detectado.")
        if st.button("➡️ Entrar al panel principal", type="primary"):
            st.session_state["authenticated"] = True
            st.rerun()
    else:
        st.info("Conecta al menos una nube para habilitar el panel.")


# ===========================================================================
# FASE 2 — Sidebar y despacho por rol
# ===========================================================================
def render_sidebar():
    st.sidebar.title("☁️ PDM Cloud Ops")
    aws_ok = st.session_state["aws_session"] is not None
    azure_ok = (st.session_state["azure_subs"] is not None
                and not st.session_state["azure_subs"].empty)
    clouds = ([("AWS")] if aws_ok else []) + (["Azure"] if azure_ok else [])
    cloud = st.sidebar.radio("Nube", clouds)

    account_label = None
    if cloud == "AWS":
        ident = st.session_state["aws_identity"] or {}
        account_label = f"{st.session_state['aws_profile']} ({ident.get('Account')})"
        st.sidebar.text_input("Región", value=st.session_state["aws_region"], key="side_region")
        st.sidebar.caption(f"Cuenta: {account_label}")
    else:
        subs = st.session_state["azure_subs"]
        total = len(subs)
        st.sidebar.markdown(f"**Alcance:** {total} suscripción(es)")
        scan_all = st.sidebar.checkbox("Consultar TODAS las suscripciones",
                                       value=True, key="az_scan_all")
        if scan_all:
            st.session_state["azure_scope"] = subs.to_dict("records")
            account_label = f"Todas ({total})"
        else:
            labels = {f"{r.displayName} ({r.subscriptionId})": r for r in subs.itertuples()}
            picked = st.sidebar.multiselect("Suscripciones", list(labels.keys()),
                                            default=list(labels.keys())[:1])
            st.session_state["azure_scope"] = [
                {"subscriptionId": labels[p].subscriptionId,
                 "displayName": labels[p].displayName} for p in picked]
            account_label = f"{len(picked)} de {total}"

    st.sidebar.markdown("---")
    role = st.sidebar.radio("Rol Operativo",
                            ["L1 — Monitoreo", "L2 — Administración", "L3 — Arquitectura"],
                            help="L1 lectura · L2 remediación/cambios · L3 estrategia")
    st.sidebar.markdown("---")
    if st.sidebar.button("🔄 Refrescar datos (limpiar caché)", use_container_width=True):
        invalidate("")
        st.rerun()
    if st.sidebar.button("🔓 Cerrar sesión", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    return cloud, role, account_label


def render_main() -> None:
    cloud, role, account_label = render_sidebar()
    st.title("Panel de Administración Operativa Cloud (PDM)")
    st.markdown(f"**Nube:** `{cloud}` | **Cuenta/Suscripción:** `{account_label}` | **Rol:** `{role}`")
    st.markdown("---")
    try:
        if cloud == "AWS":
            if role.startswith("L1"):
                aws_l1()
            elif role.startswith("L2"):
                aws_l2()
            else:
                aws_l3()
        else:
            if role.startswith("L1"):
                azure_l1()
            elif role.startswith("L2"):
                azure_l2()
            else:
                azure_l3()
    except aws.AWSAuthError as exc:
        auth_error(str(exc))
    except azure.AzureAuthError as exc:
        auth_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Error inesperado: {exc}")


def _scope():
    return st.session_state.get("azure_scope") or []


def _run_all(fn, *args, **kwargs):
    cred = st.session_state["azure_credential"]
    return azure.run_all_subscriptions(fn, cred, _scope(), *args, **kwargs)


# ===========================================================================
# AWS — Paneles por nivel
# ===========================================================================
def aws_l1() -> None:
    s = st.session_state["aws_session"]
    region = st.session_state.get("side_region") or st.session_state["aws_region"]
    diario, semanal, mensual, kpis = st.tabs(
        ["📅 Diario", "🗓️ Semanal", "📆 Mensual", "📊 KPIs"])

    with diario:
        st.subheader("1.1 Salud de Infraestructura — AWS Health")
        show_table(cached(f"aws_health_{region}", aws.l1_health_events, s), "aws_health")
        st.subheader("1.3 Seguridad — Security Hub (CRITICAL/HIGH)")
        show_table(cached("aws_sechub", aws.l1_security_findings, s), "aws_sechub")
        st.subheader("Alertas — CloudWatch en ALARM")
        show_table(cached(f"aws_alarms_{region}", aws.l1_cloudwatch_alarms, s, region), "aws_alarms")

    with semanal:
        st.subheader("1.6 Recursos Huérfanos — EBS / EIP / LB")
        show_table(cached(f"aws_orphan_{region}", aws.l1_orphaned_resources, s, region), "aws_orphan")
        st.subheader("1.7 Respaldo de Servicios — AWS Backup")
        show_table(cached(f"aws_backup_{region}", aws.l1_backup_status, s, region), "aws_backup")
        st.subheader("1.5 Servicios Deprecados / Retiros")
        show_table(cached("aws_retire", aws.l1_service_retirements, s), "aws_retire")

    with mensual:
        st.subheader("1.4 Costos — Últimos 7 días")
        df7 = cached("aws_cost7", aws.l1_cost_last_7_days, s)
        show_table(df7, "aws_cost7")
        cost_chart(df7, "Top servicios (7 días)")
        st.subheader("1.4 Costos — Mes actual")
        show_table(cached("aws_costm", aws.l1_cost_current_month, s), "aws_costm")

    with kpis:
        _aws_kpi_form_l1()


def _aws_kpi_form_l1() -> None:
    st.subheader("Registro de métricas L1")
    d = st.session_state["kpi_l1"]
    c = st.columns(4)
    d["alerts_total"] = c[0].number_input("Alertas totales", 0, value=d.get("alerts_total", 0))
    d["alerts_ack"] = c[1].number_input("Alertas reconocidas", 0, value=d.get("alerts_ack", 0))
    d["sum_ack_minutes"] = c[2].number_input("Σ min. reconocimiento", 0.0, value=d.get("sum_ack_minutes", 0.0))
    d["proactive_alerts"] = c[3].number_input("Alertas proactivas", 0, value=d.get("proactive_alerts", 0))
    c2 = st.columns(3)
    d["incidents_resolved"] = c2[0].number_input("Incidentes resueltos", 0, value=d.get("incidents_resolved", 0))
    d["sum_resolution_minutes"] = c2[1].number_input("Σ min. resolución", 0.0, value=d.get("sum_resolution_minutes", 0.0))
    d["false_positives"] = c2[2].number_input("Falsos positivos", 0, value=d.get("false_positives", 0))
    results = kpi_engine.kpis_l1(**d)
    render_kpis(results, "KPIs L1 — Eficiencia / Calidad / Proactividad")


def aws_l2() -> None:
    s = st.session_state["aws_session"]
    region = st.session_state.get("side_region") or st.session_state["aws_region"]
    st.warning("⚠️ **L2 — Modo cambios/remediación:** confirma cada acción destructiva.")
    validacion, parches, mant, kpis = st.tabs(
        ["📥 Validación L1", "🩹 Upgrades/Parches", "🛠️ Mantenimiento", "📊 KPIs"])

    with validacion:
        st.subheader("1.1 Bandeja de hallazgos escalados (Security Hub)")
        findings = cached("aws_sechub", aws.l1_security_findings, s)
        show_table(findings, "aws_sechub_l2")
        if findings is not None and not findings.empty:
            idx = st.selectbox("Selecciona hallazgo", findings.index,
                               format_func=lambda i: f"{findings.loc[i,'severity']} — {findings.loc[i,'title']}")
            status = st.selectbox("Acción", ["RESOLVED", "NOTIFIED", "SUPPRESSED"])
            product_arn = st.text_input("ProductArn")
            if st.checkbox("Confirmo la remediación", key="aws_sec_confirm") and \
               st.button("🟠 Aplicar"):
                res = aws.l2_set_finding_workflow(s, findings.loc[idx, "findingId"], product_arn, status)
                (st.success if res["ok"] else st.error)(res["message"])

    with parches:
        st.subheader("1.2 Parches disponibles (SSM Patch Manager)")
        patches = cached(f"aws_patch_{region}", aws.l2_available_patches, s, region)
        show_table(patches, "aws_patch")
        if patches is not None and not patches.empty:
            noncompliant = patches[patches["complianceStatus"] == "NON_COMPLIANT"]
            ids = st.multiselect("Instancias a parchear",
                                 noncompliant["instanceId"].tolist() if not noncompliant.empty else [])
            if ids and st.checkbox("Confirmo el parcheo", key="aws_patch_confirm") and \
               st.button("🩹 Ejecutar RunPatchBaseline"):
                res = aws.l2_run_patch_baseline(s, ids, region)
                (st.success if res["ok"] else st.error)(res["message"])

    with mant:
        st.subheader("1.5 Ventanas de Mantenimiento (SSM)")
        show_table(cached(f"aws_mw_{region}", aws.l2_maintenance_windows, s, region), "aws_mw")
        with st.expander("➕ Crear ventana de mantenimiento"):
            name = st.text_input("Nombre", key="mw_name")
            sched = st.text_input("Schedule (cron)", value="cron(0 2 ? * SUN *)", key="mw_sched")
            dur = st.number_input("Duración (h)", 1, 24, 2, key="mw_dur")
            cutoff = st.number_input("Cutoff (h)", 0, 23, 1, key="mw_cut")
            if st.button("Crear ventana"):
                res = aws.l2_create_maintenance_window(s, name, sched, int(dur), int(cutoff), region)
                (st.success if res["ok"] else st.error)(res["message"])

    with kpis:
        _kpi_form_l2("aws")


def aws_l3() -> None:
    s = st.session_state["aws_session"]
    arqui, optim, kpis = st.tabs(["🏛️ Arquitectura", "📈 Optimización", "📊 KPIs"])
    with arqui:
        st.subheader("1.1 Well-Architected — estándares de Security Hub")
        show_table(cached("aws_waf", aws.l3_well_architected_security, s), "aws_waf")
        st.subheader("1.2 Playbooks de automatización (SSM Documents)")
        show_table(cached("aws_playbooks", aws.l3_automation_playbooks, s), "aws_playbooks")
    with optim:
        st.subheader("1.4 Right-Sizing — Compute Optimizer")
        show_table(cached("aws_rightsizing", aws.l3_compute_optimizer, s), "aws_rightsizing")
        st.subheader("1.4 Proyección financiera — Savings Plans")
        show_table(cached("aws_savings", aws.l3_savings_plans_recommendations, s), "aws_savings")
        st.subheader("1.5 Límites de Servicio (Quotas)")
        show_table(cached("aws_quotas", aws.l3_service_quotas, s), "aws_quotas")
    with kpis:
        _kpi_form_l3("aws")


# ===========================================================================
# Azure — Paneles por nivel (multi-suscripción, en paralelo)
# ===========================================================================
def azure_l1() -> None:
    scope = _scope()
    st.caption(f"Alcance: **{len(scope)}** suscripción(es). Barrido paralelo en segundo plano.")
    diario, semanal, mensual, kpis = st.tabs(
        ["📅 Diario", "🗓️ Semanal", "📆 Mensual", "📊 KPIs"])

    with diario:
        st.subheader("1.1 Salud — Service Health")
        show_table(cached("az_health", _run_all, azure.l1_service_health), "az_health")
        st.subheader("1.3 Seguridad — Secure Score / Alertas Defender")
        show_table(cached("az_score", _run_all, azure.l1_secure_score), "az_score")
        show_table(cached("az_secalerts", _run_all, azure.l1_security_alerts), "az_secalerts")
        st.subheader("Alertas — Azure Monitor (Sev 0/1/2)")
        show_table(cached("az_monalerts", _run_all, azure.l1_monitor_alerts), "az_monalerts")

    with semanal:
        st.subheader("1.6 Recursos Huérfanos — Discos / IPs / LB")
        show_table(cached("az_orphan", _run_all, azure.l1_orphaned_resources), "az_orphan")
        st.subheader("1.7 Respaldo — Recovery Services Vaults")
        show_table(cached("az_backup", _run_all, azure.l1_backup_status), "az_backup")
        st.subheader("1.5 Servicios Deprecados — Advisor")
        show_table(cached("az_retire", _run_all, azure.l1_service_retirements), "az_retire")

    with mensual:
        st.subheader("1.4 Costos — Últimos 7 días")
        df7 = cached("az_cost7", _run_all, azure.l1_cost_last_7_days)
        show_table(df7, "az_cost7")
        cost_chart(df7, "Top servicios (7 días)")
        st.subheader("1.4 Costos — Mes actual (consolidado)")
        dfm = cached("az_costm", _run_all, azure.l1_cost_current_month)
        show_table(dfm, "az_costm")
        cost_by_sub_chart(dfm)

    with kpis:
        _aws_kpi_form_l1_shared()


def _aws_kpi_form_l1_shared() -> None:
    # KPIs L1 son comunes a ambas nubes (métricas operativas del servicio)
    _aws_kpi_form_l1()


def azure_l2() -> None:
    scope = _scope()
    st.warning("⚠️ **L2 — Modo cambios/remediación:** cada recurso muestra su suscripción. Confirma antes.")
    validacion, upgrades, kpis = st.tabs(["📥 Validación L1", "🩹 Upgrades/Parches", "📊 KPIs"])

    with validacion:
        st.subheader("1.1 Bandeja de recursos huérfanos escalados")
        df = cached("az_orphan", _run_all, azure.l1_orphaned_resources)
        show_table(df, "az_orphan_l2")
        if df is not None and not df.empty:
            idx = st.selectbox("Recurso a remediar", df.index,
                               format_func=lambda i: f"[{df.loc[i,'subscriptionName']}] {df.loc[i,'type']} — {df.loc[i,'name']}")
            target = df.loc[idx]
            st.info(f"Suscripción destino: **{target['subscriptionName']}**")
            if st.checkbox("Confirmo la acción destructiva", key="az_orphan_confirm") and \
               st.button("🔴 Remediar"):
                res = _azure_remediate(target["subscriptionId"], target)
                (st.success if res["ok"] else st.error)(res["message"])

    with upgrades:
        st.subheader("1.2 Actualizaciones disponibles (Advisor)")
        show_table(cached("az_updates", _run_all, azure.l2_available_updates), "az_updates")

    with kpis:
        _kpi_form_l2("az")


def _azure_remediate(sub_id, target) -> dict:
    cred = st.session_state["azure_credential"]
    rtype, rid = target["type"], target["resourceId"]
    if rtype.startswith("ManagedDisk"):
        return azure.l2_delete_disk(cred, sub_id, rid)
    if rtype.startswith("PublicIP"):
        return azure.l2_delete_public_ip(cred, sub_id, rid)
    if rtype.startswith("LoadBalancer"):
        return azure.l2_delete_load_balancer(cred, sub_id, rid)
    return {"ok": False, "message": "Tipo no soportado."}


def azure_l3() -> None:
    scope = _scope()
    arqui, optim, kpis = st.tabs(["🏛️ Arquitectura", "📈 Optimización", "📊 KPIs"])
    with arqui:
        st.subheader("1.1 Well-Architected — controles de Defender")
        show_table(cached("az_waf", _run_all, azure.l3_well_architected_security), "az_waf")
        st.subheader("1.2 Playbooks — Automation Accounts")
        show_table(cached("az_playbooks", _run_all, azure.l3_automation_playbooks), "az_playbooks")
    with optim:
        st.subheader("1.4 Right-Sizing y ahorro — Advisor")
        show_table(cached("az_rightsizing", _run_all, azure.l3_rightsizing_recommendations), "az_rightsizing")
        st.subheader("1.4 Proyección financiera — Reservas")
        show_table(cached("az_reservations", _run_all, azure.l3_reservation_recommendations), "az_reservations")
        st.subheader("1.5 Cuotas de cómputo")
        loc = st.text_input("Región", value="eastus", key="az_quota_loc")
        show_table(cached(f"az_quotas_{loc}", _run_all, azure.l3_service_quotas, loc), "az_quotas")
    with kpis:
        _kpi_form_l3("az")


# ===========================================================================
# Formularios de KPI compartidos L2 / L3
# ===========================================================================
def _kpi_form_l2(prefix: str) -> None:
    st.subheader("Registro de métricas L2")
    d = st.session_state["kpi_l2"]
    c = st.columns(3)
    d["escalations_total"] = c[0].number_input("Escalamientos totales", 0, value=d.get("escalations_total", 0), key=f"{prefix}_esc_t")
    d["escalations_ok"] = c[1].number_input("Escalamientos sin error", 0, value=d.get("escalations_ok", 0), key=f"{prefix}_esc_ok")
    d["changes_total"] = c[2].number_input("Cambios totales", 0, value=d.get("changes_total", 0), key=f"{prefix}_chg_t")
    c2 = st.columns(3)
    d["changes_compliant"] = c2[0].number_input("Cambios conformes", 0, value=d.get("changes_compliant", 0), key=f"{prefix}_chg_ok")
    d["cost_baseline"] = c2[1].number_input("Costo baseline", 0.0, value=d.get("cost_baseline", 0.0), key=f"{prefix}_cost_b")
    d["cost_current"] = c2[2].number_input("Costo actual", 0.0, value=d.get("cost_current", 0.0), key=f"{prefix}_cost_c")
    render_kpis(kpi_engine.kpis_l2(**d), "KPIs L2 — Gestión / Cumplimiento / Costos")


def _kpi_form_l3(prefix: str) -> None:
    st.subheader("Registro de métricas L3")
    d = st.session_state["kpi_l3"]
    c = st.columns(3)
    d["automations_planned"] = c[0].number_input("Automatizaciones planificadas", 0, value=d.get("automations_planned", 0), key=f"{prefix}_auto_p")
    d["automations_done"] = c[1].number_input("Automatizaciones hechas", 0, value=d.get("automations_done", 0), key=f"{prefix}_auto_d")
    d["projects_total"] = c[2].number_input("Proyectos totales", 0, value=d.get("projects_total", 0), key=f"{prefix}_proj_t")
    c2 = st.columns(3)
    d["projects_no_incident"] = c2[0].number_input("Proyectos sin incidentes", 0, value=d.get("projects_no_incident", 0), key=f"{prefix}_proj_ok")
    d["perf_before"] = c2[1].number_input("Rendimiento antes", 0.0, value=d.get("perf_before", 0.0), key=f"{prefix}_perf_b")
    d["perf_after"] = c2[2].number_input("Rendimiento después", 0.0, value=d.get("perf_after", 0.0), key=f"{prefix}_perf_a")
    render_kpis(kpi_engine.kpis_l3(**d), "KPIs L3 — Automatización / Calidad / Rendimiento")


# ===========================================================================
# Entry point
# ===========================================================================
def main() -> None:
    if not st.session_state.get("authenticated"):
        render_login()
    else:
        render_main()


if __name__ == "__main__":
    main()
