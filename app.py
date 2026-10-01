"""
app.py  —  Automatización PDM
==============================
Panel de Control Maestro multicloud (AWS + Azure).
Tema corporativo: fondo blanco, azul Kyndryl, sin emojis.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import aws_core as aws
import azure_core as azure
import cloud_health as ch
import kpi_metrics as kpm

try:
    import plotly.express as px
    import plotly.graph_objects as go
    _PLOTLY = True
except Exception:  # noqa: BLE001
    _PLOTLY = False

# Colores corporativos (sin emojis)
_STATUS_COLOR = {"green": "#1a7a4a", "orange": "#c87800", "red": "#c0392b", "gray": "#6b6b6b"}
_SEV_BG = {
    "OK": "#d4edda", "OPERATIONAL": "#d4edda",
    "DEGRADED": "#fff3cd", "WARNING": "#fff3cd", "ADVISORY": "#fff3cd",
    "DISRUPTED": "#f8d7da", "IMPAIRED": "#f8d7da",
    "UNKNOWN": "#e2e3e5",
}
_SEV_FG = {
    "OK": "#155724", "OPERATIONAL": "#155724",
    "DEGRADED": "#856404", "WARNING": "#856404", "ADVISORY": "#856404",
    "DISRUPTED": "#721c24", "IMPAIRED": "#721c24",
    "UNKNOWN": "#383d41",
}
_SEV_LABEL = {
    "OK": "Operacional", "OPERATIONAL": "Operacional",
    "DEGRADED": "Degradado", "WARNING": "Advertencia", "ADVISORY": "Informativo",
    "DISRUPTED": "Interrupcion", "IMPAIRED": "Afectado",
    "UNKNOWN": "Desconocido",
}

# ── Logo path ──────────────────────────────────────────────────────────────────
_LOGO_PATH = Path(__file__).parent / "Logo.svg"

st.set_page_config(
    page_title="Automatizacion PDM | Kyndryl",
    page_icon=str(_LOGO_PATH) if _LOGO_PATH.exists() else None,
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inyección de CSS corporativo (tema claro, tipografía limpia)
st.markdown("""
<style>
/* Fuente y fondo general */
html, body, [class*="css"] {
    font-family: 'Inter', 'Segoe UI', sans-serif;
    background-color: #FFFFFF;
    color: #1A1A2E;
}
/* Header de página */
.pdm-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.5rem 0 1rem 0;
    border-bottom: 2px solid #0050A0;
    margin-bottom: 1.5rem;
}
.pdm-header img { height: 36px; }
.pdm-header h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: #0050A0;
    margin: 0;
}
/* Tarjetas de estado */
.status-card {
    border-radius: 6px;
    padding: 8px 10px;
    text-align: center;
    margin-bottom: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    line-height: 1.3;
}
/* Tabla de métricas */
[data-testid="stMetric"] {
    background: #F4F6F9;
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    border-left: 4px solid #0050A0;
}
/* Sidebar */
section[data-testid="stSidebar"] {
    background: #F4F6F9;
    border-right: 1px solid #dde1ea;
}
/* Botones primarios */
[data-testid="stButton"] > button[kind="primary"] {
    background: #0050A0;
    color: white;
    border: none;
    border-radius: 4px;
}
[data-testid="stButton"] > button[kind="primary"]:hover {
    background: #003d80;
}
/* Tabs */
[data-testid="stTabs"] button {
    font-weight: 600;
    color: #0050A0;
}
/* Títulos de sección */
h1, h2, h3 { color: #0050A0; }
/* Alertas con color corporativo */
.stAlert { border-radius: 6px; }
/* Expander */
[data-testid="stExpander"] summary {
    font-weight: 600;
    color: #0050A0;
}
</style>
""", unsafe_allow_html=True)


# ── Logo en sidebar y header ───────────────────────────────────────────────────
def _show_logo_sidebar():
    if _LOGO_PATH.exists():
        st.sidebar.image(str(_LOGO_PATH), width=140)
    st.sidebar.markdown("---")


def _page_header(title: str, subtitle: str = ""):
    logo_b64 = ""
    if _LOGO_PATH.exists():
        import base64
        logo_b64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode()
        logo_tag = (f'<img src="data:image/svg+xml;base64,{logo_b64}" '
                    f'alt="Kyndryl" style="height:34px">')
    else:
        logo_tag = '<span style="font-weight:900;color:#ff462d;font-size:1.1rem">Kyndryl</span>'
    st.markdown(
        f"""<div class="pdm-header">
            {logo_tag}
            <div>
                <h1 style="margin:0;font-size:1.3rem;color:#0050A0">{title}</h1>
                {"<p style='margin:0;font-size:0.8rem;color:#6b6b6b'>" + subtitle + "</p>" if subtitle else ""}
            </div>
        </div>""",
        unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Estado de sesion
# ══════════════════════════════════════════════════════════════════════════════
_INIT = {
    "aws_session": None, "aws_profile": None, "aws_identity": None,
    "aws_region": "us-east-1",
    "azure_cred": None, "azure_subs": None, "azure_scope": [],
    "auth_done": False,
    "cache": {},
    "kpi_data": {},
    "incidents": [],
    "lifecycle": {},
}
for _k, _v in _INIT.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ══════════════════════════════════════════════════════════════════════════════
# Cache liviano
# ══════════════════════════════════════════════════════════════════════════════
def cached(key: str, fn, *args, **kwargs):
    c = st.session_state["cache"]
    if key not in c:
        with st.spinner("Consultando..."):
            c[key] = fn(*args, **kwargs)
    return c[key]


def bust(prefix: str = "") -> None:
    st.session_state["cache"] = {
        k: v for k, v in st.session_state["cache"].items()
        if not k.startswith(prefix)
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de UI
# ══════════════════════════════════════════════════════════════════════════════
def export_btns(df: pd.DataFrame, prefix: str) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    c1, c2 = st.columns(2)
    with c1:
        buf = io.StringIO()
        (df if df is not None else pd.DataFrame()).to_csv(buf, index=False)
        st.download_button("Descargar CSV", buf.getvalue().encode("utf-8-sig"),
                           f"{prefix}_{ts}.csv", "text/csv", key=f"csv_{prefix}_{ts}")
    with c2:
        xb = io.BytesIO()
        try:
            with pd.ExcelWriter(xb, engine="openpyxl") as w:
                (df if df is not None else pd.DataFrame()).to_excel(
                    w, index=False, sheet_name="datos")
            st.download_button("Descargar Excel", xb.getvalue(),
                               f"{prefix}_{ts}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key=f"xl_{prefix}_{ts}")
        except Exception:  # noqa: BLE001
            pass


def show_df(df: pd.DataFrame, prefix: str, empty_msg: str = "Sin resultados.") -> None:
    note    = getattr(df, "attrs", {}).get("note")
    scanned = getattr(df, "attrs", {}).get("subscriptions_scanned")
    errors  = getattr(df, "attrs", {}).get("errors") or []
    if scanned:
        st.caption(f"Suscripciones consultadas: {scanned}")
    if note:
        st.info(note)
    if df is None or df.empty:
        st.warning(empty_msg)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        if "subscriptionName" in df.columns:
            agg = (df.groupby("subscriptionName").size()
                     .reset_index(name="hallazgos")
                     .sort_values("hallazgos", ascending=False))
            with st.expander(f"Resumen por suscripcion — {len(agg)} con hallazgos"):
                st.dataframe(agg, use_container_width=True, hide_index=True)
        st.caption(f"Total registros: {len(df)}")
    if errors:
        with st.expander(f"Advertencia: {len(errors)} suscripcion(es) con errores"):
            for e in errors:
                st.text(f"- {e}")
    export_btns(df if df is not None else pd.DataFrame(), prefix)


def action_result(res: dict) -> None:
    (st.success if res.get("ok") else st.error)(res["message"])


def auth_err(msg: str) -> None:
    st.error(f"Sesion expirada. {msg}. Vuelve al login y reautenticate.")


def _aws():          return st.session_state["aws_session"]
def _region():       return st.session_state.get("side_region") or st.session_state["aws_region"]
def _cred():         return st.session_state["azure_cred"]
def _scope():        return st.session_state.get("azure_scope") or []
def _is_aws():       return st.session_state.get("active_cloud") == "AWS"

def _run_all(fn, *args, **kwargs):
    return azure.run_all(fn, _cred(), _scope(), *args, **kwargs)

def _bar(df, x, y, title):
    if df is None or df.empty or x not in df.columns:
        return
    agg = df.groupby(x, as_index=False)[y].sum().sort_values(y, ascending=False).head(15)
    if _PLOTLY:
        fig = px.bar(agg, x=y, y=x, orientation="h", title=title,
                     color_discrete_sequence=["#0050A0"])
        fig.update_layout(yaxis=dict(autorange="reversed"), height=400,
                          plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.bar_chart(agg.set_index(x))


# ══════════════════════════════════════════════════════════════════════════════
# FASE 1 — Login
# ══════════════════════════════════════════════════════════════════════════════
def render_login() -> None:
    _page_header("Automatizacion PDM",
                 "Plan de Administracion Operativa Cloud — Kyndryl Cloud Operations")

    # Vista previa Cloud Health (publica, sin autenticacion)
    with st.expander("Estado actual de la plataforma cloud (publico, sin autenticacion)",
                     expanded=True):
        _cloud_health_preview()

    st.markdown("---")
    col_aws, col_az = st.columns(2)

    with col_aws:
        st.subheader("AWS — IAM Identity Center (SSO)")
        prof_df = aws.describe_profiles()
        if prof_df.empty:
            st.warning("Sin perfiles en ~/.aws/config. Ejecuta 'aws configure sso'.")
            opts = [st.text_input("Perfil manual", key="aws_manual_login")]
        else:
            with st.expander("Perfiles detectados en ~/.aws/config"):
                st.dataframe(prof_df, use_container_width=True, hide_index=True)
            opts = prof_df["profile"].tolist()
        profile = st.selectbox("Perfil SSO", opts or ["sin perfiles"], key="aws_sel")
        region  = st.text_input("Region", value=st.session_state["aws_region"], key="aws_reg")
        c1, c2  = st.columns(2)
        with c1:
            if st.button("Ejecutar aws sso login", use_container_width=True):
                if profile not in ("sin perfiles", ""):
                    r = aws.sso_login(profile)
                    (st.success if r["ok"] else st.error)(r["message"])
        with c2:
            if st.button("Conectar AWS", type="primary", use_container_width=True):
                if profile not in ("sin perfiles", ""):
                    try:
                        with st.spinner("Conectando..."):
                            s = aws.get_session(profile, region)
                            ident = aws.whoami(s)
                        st.session_state.update({
                            "aws_session": s, "aws_profile": profile,
                            "aws_identity": ident, "aws_region": region})
                        st.success(f"Cuenta {ident.get('Account')} conectada.")
                    except aws.AWSAuthError as e:
                        st.error(str(e))
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Error: {e}")
        if st.session_state["aws_identity"]:
            st.info(f"Conectado — Cuenta: {st.session_state['aws_identity'].get('Account')}")

    with col_az:
        st.subheader("Azure — Autenticacion interactiva")
        method = st.radio("Metodo", ["Interactivo (navegador)", "az login", "Service Principal"],
                          key="az_method_login")
        mode = {"Interactivo (navegador)": "interactive",
                "az login": "default",
                "Service Principal": "sp"}[method]
        tenant_id = None
        if mode in ("interactive", "default"):
            tenant_id = st.text_input(
                "Tenant ID (recomendado)",
                key="az_tid",
                help="Especifica el tenant que contiene tus suscripciones.").strip() or None
        sp_t = sp_c = sp_s = None
        if mode == "sp":
            sp_t = st.text_input("Tenant ID", key="sp_t")
            sp_c = st.text_input("Client ID", key="sp_c")
            sp_s = st.text_input("Client Secret", type="password", key="sp_s")
        if st.button("Conectar Azure", type="primary", use_container_width=True):
            try:
                with st.spinner("Autenticando en Azure..."):
                    cred = azure.get_credential(
                        mode=mode,
                        tenant_id=sp_t if mode == "sp" else tenant_id,
                        client_id=sp_c, client_secret=sp_s)
                    subs = azure.discover_subscriptions(cred)
                if subs.empty:
                    st.warning("Sin suscripciones detectadas. Verifica el Tenant ID.")
                    try:
                        tenants = azure.discover_tenants(cred)
                        if not tenants.empty:
                            st.markdown("**Tenants disponibles para tu cuenta:**")
                            st.dataframe(tenants, use_container_width=True, hide_index=True)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    st.session_state.update({"azure_cred": cred, "azure_subs": subs})
                    st.success(f"{len(subs)} suscripcion(es) encontradas.")
            except azure.AzureAuthError as e:
                st.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.error(f"Error: {e}")
        if st.session_state["azure_subs"] is not None and \
                not st.session_state["azure_subs"].empty:
            st.dataframe(st.session_state["azure_subs"],
                         use_container_width=True, hide_index=True)

    st.markdown("---")
    aws_ok = st.session_state["aws_session"] is not None
    az_ok  = (st.session_state["azure_subs"] is not None
              and not st.session_state["azure_subs"].empty)
    if aws_ok or az_ok:
        st.success("Entorno valido detectado. Puedes acceder al panel.")
        if st.button("Acceder al Panel de Control Maestro", type="primary",
                     use_container_width=True):
            st.session_state["auth_done"] = True
            st.rerun()
    else:
        st.info("Conecta al menos una nube para continuar.")


def _cloud_health_preview() -> None:
    all_df  = cached("ch_preview", ch.fetch_all_health)
    inc     = ch.active_incidents(all_df)
    ok_cnt  = len(all_df[all_df["severity"] == "OK"]) if not all_df.empty else 0
    warn_cnt = len(all_df[all_df["severity"].isin(["DEGRADED","WARNING","ADVISORY"])]) if not all_df.empty else 0
    inc_cnt = len(inc)

    c1, c2, c3 = st.columns(3)
    c1.metric("Regiones operacionales", ok_cnt)
    c2.metric("Regiones con advertencia", warn_cnt)
    c3.metric("Incidentes activos", inc_cnt)

    if inc_cnt:
        st.error(f"Hay {inc_cnt} incidente(s) activo(s) en la plataforma cloud.")
        cols = ["provider", "region", "severity", "title", "updated"]
        cols = [c for c in cols if c in inc.columns]
        st.dataframe(inc[cols], use_container_width=True, hide_index=True)
    else:
        st.success("Todas las regiones de AWS y Azure reportan estado Operational.")


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar del panel principal
# ══════════════════════════════════════════════════════════════════════════════
AREAS = [
    "1.1  Cloud Health",
    "1.2  Inventario y Monitoreo de Recursos",
    "1.3  Seguridad y Cumplimiento",
    "1.4  Ciclo de Vida (Deprecados)",
    "KPIs  Centro de Mando",
]


def render_sidebar() -> str:
    _show_logo_sidebar()
    st.sidebar.markdown("### Automatizacion PDM")
    st.sidebar.caption("Kyndryl Cloud Operations")

    aws_ok = st.session_state["aws_session"] is not None
    az_ok  = (st.session_state["azure_subs"] is not None
              and not st.session_state["azure_subs"].empty)
    clouds = (["AWS"] if aws_ok else []) + (["Azure"] if az_ok else [])

    if clouds:
        cloud = st.sidebar.radio("Nube activa", clouds, key="active_cloud")
        if cloud == "AWS":
            ident = st.session_state["aws_identity"] or {}
            st.sidebar.text_input("Region",
                                  value=st.session_state["aws_region"],
                                  key="side_region")
            st.sidebar.caption(f"Cuenta: {ident.get('Account', '—')}")
        else:
            subs  = st.session_state["azure_subs"]
            total = len(subs)
            scan_all = st.sidebar.checkbox("Todas las suscripciones",
                                           value=True, key="az_all")
            if scan_all:
                st.session_state["azure_scope"] = subs.to_dict("records")
                st.sidebar.caption(f"Alcance: {total} suscripciones")
            else:
                lmap = {f"{r.displayName}": r for r in subs.itertuples()}
                picks = st.sidebar.multiselect("Suscripciones",
                                               list(lmap.keys()),
                                               default=list(lmap.keys())[:1])
                st.session_state["azure_scope"] = [
                    {"subscriptionId": lmap[p].subscriptionId,
                     "displayName": lmap[p].displayName}
                    for p in picks]
                st.sidebar.caption(f"Alcance: {len(picks)} de {total}")
    else:
        st.sidebar.warning("Sin nube conectada.")

    st.sidebar.markdown("---")
    area = st.sidebar.radio("Area de Gestion", AREAS)
    st.sidebar.markdown("---")

    if st.sidebar.button("Actualizar datos", use_container_width=True):
        bust("")
        st.rerun()
    if st.sidebar.button("Cerrar sesion / Volver al Login",
                         use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    return area


# ══════════════════════════════════════════════════════════════════════════════
# 1.1 — CLOUD HEALTH
# ══════════════════════════════════════════════════════════════════════════════
def area_cloud_health() -> None:
    _page_header(
        "1.1 — Estado de Salud de la Plataforma Cloud",
        "Fuente: health.aws.amazon.com/health/status | azure.status.microsoft/en-us/status/feed")

    col_ref, col_ts = st.columns([1, 4])
    with col_ref:
        if st.button("Actualizar estado"):
            bust("ch_")
            st.rerun()

    all_df  = cached("ch_all", ch.fetch_all_health)
    inc_df  = ch.active_incidents(all_df)
    fetched = all_df.attrs.get("fetched_at", "")

    with col_ts:
        st.caption(f"Ultima consulta: {fetched}")

    ok_cnt   = len(all_df[all_df["severity"] == "OK"]) if not all_df.empty else 0
    warn_cnt = len(all_df[all_df["severity"].isin(["DEGRADED","WARNING","ADVISORY","INFO"])]) if not all_df.empty else 0
    inc_cnt  = len(inc_df)
    total    = len(all_df)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Regiones monitoreadas", total)
    m2.metric("Operacional",           ok_cnt)
    m3.metric("Con advertencia",       warn_cnt)
    m4.metric("Incidentes activos",    inc_cnt)

    if inc_cnt:
        st.error(
            f"ALERTA: {inc_cnt} incidente(s) activo(s) en la plataforma cloud. "
            "Revisar detalles y escalar a Nivel 2 si hay impacto en clientes.")
    else:
        st.success(
            "Estado: Todas las regiones AWS y Azure reportan Operational. "
            "Sin accion requerida.")

    st.markdown("---")

    tab_inc, tab_aws, tab_azure, tab_reg = st.tabs(
        ["Incidentes activos", "AWS por region",
         "Azure por region", "Registrar hallazgo"])

    with tab_inc:
        if inc_df.empty:
            st.success("Sin incidentes activos en este momento.")
        else:
            for _, row in inc_df.iterrows():
                sev = row.get("severity", "UNKNOWN")
                bg  = _SEV_BG.get(sev, "#e2e3e5")
                fg  = _SEV_FG.get(sev, "#383d41")
                lbl = _SEV_LABEL.get(sev, sev)
                with st.container(border=True):
                    c1, c2 = st.columns([1, 6])
                    with c1:
                        st.markdown(
                            f'<div style="background:{bg};color:{fg};padding:6px 10px;'
                            f'border-radius:6px;text-align:center;font-weight:700;'
                            f'font-size:0.85rem">{lbl}</div>',
                            unsafe_allow_html=True)
                    with c2:
                        st.markdown(
                            f"**{row.get('provider','—')} — {row.get('region','—')}**  "
                            f"| *{row.get('service','—')}*")
                        st.write(row.get("title", ""))
                        if row.get("link"):
                            st.markdown(f"[Ver detalle en portal]({row['link']})")
                        st.caption(f"Actualizado: {row.get('updated','')}")
        export_btns(inc_df if not inc_df.empty else pd.DataFrame(), "health_incidents")

    with tab_aws:
        aws_df = all_df[all_df["provider"] == "AWS"] if not all_df.empty else pd.DataFrame()
        _render_health_grid(aws_df, "AWS")

    with tab_azure:
        az_df = all_df[all_df["provider"] == "Azure"] if not all_df.empty else pd.DataFrame()
        _render_health_grid(az_df, "Azure")

    with tab_reg:
        _health_register_incident()


def _render_health_grid(df: pd.DataFrame, provider: str) -> None:
    if df is None or df.empty:
        st.info(f"Sin datos de {provider}.")
        return

    fc, _ = st.columns([2, 3])
    with fc:
        filter_sev = st.selectbox(
            "Filtrar", ["Todos", "Solo con incidentes", "Solo operacional"],
            key=f"filter_{provider}")

    if filter_sev == "Solo con incidentes":
        df = df[~df["severity"].isin(["OK", "OPERATIONAL"])]
    elif filter_sev == "Solo operacional":
        df = df[df["severity"].isin(["OK", "OPERATIONAL"])]

    if df.empty:
        st.info("Sin resultados para este filtro.")
        return

    n_cols = 5
    groups = [df.iloc[i:i+n_cols] for i in range(0, len(df), n_cols)]
    for group in groups:
        cols = st.columns(n_cols)
        for col, (_, row) in zip(cols, group.iterrows()):
            with col:
                sev = row.get("severity", "UNKNOWN")
                bg  = _SEV_BG.get(sev, "#e2e3e5")
                fg  = _SEV_FG.get(sev, "#383d41")
                lbl = _SEV_LABEL.get(sev, sev)
                st.markdown(
                    f'<div class="status-card" style="background:{bg};color:{fg}">'
                    f'{row.get("region","")}<br>'
                    f'<span style="font-size:0.65rem">{lbl}</span>'
                    f'</div>',
                    unsafe_allow_html=True)
                if sev not in ("OK", "OPERATIONAL"):
                    st.caption(row.get("title","")[:60])

    with st.expander(f"Ver tabla completa de {provider} ({len(df)} regiones)"):
        cols_show = [c for c in ["region","service","severity","title","updated"]
                     if c in df.columns]
        st.dataframe(df[cols_show], use_container_width=True, hide_index=True)
    export_btns(df, f"health_{provider.lower()}")


def _health_register_incident() -> None:
    st.subheader("Registrar hallazgo y escalar a Nivel 2")
    st.caption("Segun el manual PDM: documenta fecha, servicio afectado, region, estado y observaciones.")
    with st.form("health_incident_form"):
        c1, c2 = st.columns(2)
        provider  = c1.selectbox("Proveedor", ["AWS", "Azure"])
        region    = c2.text_input("Region afectada")
        service   = st.text_input("Servicio afectado")
        estado    = st.selectbox("Estado detectado",
                                 ["Interrupcion (Outage)", "Degradado (Warning)",
                                  "Resuelto", "Informativo"])
        obs       = st.text_area("Observaciones / impacto en clientes")
        escalate  = st.checkbox("Escalar a Nivel 2")
        submitted = st.form_submit_button("Registrar hallazgo")
        if submitted:
            st.session_state["incidents"].append({
                "ts": datetime.now().isoformat(),
                "provider": provider, "region": region, "service": service,
                "estado": estado, "obs": obs, "escalado": escalate,
                "status": "Abierto", "close_ts": None})
            st.success("Hallazgo registrado." +
                       (" Escalamiento marcado." if escalate else ""))

    if st.session_state["incidents"]:
        st.subheader("Hallazgos registrados")
        df_i = pd.DataFrame(st.session_state["incidents"])
        st.dataframe(df_i, use_container_width=True, hide_index=True)
        export_btns(df_i, "health_hallazgos")

        open_inc = [i for i in st.session_state["incidents"] if i["status"] == "Abierto"]
        if open_inc:
            idx = st.selectbox("Cerrar hallazgo", range(len(open_inc)),
                               format_func=lambda i: (
                                   f"{open_inc[i]['provider']} | "
                                   f"{open_inc[i]['region']} | "
                                   f"{open_inc[i]['service']}"))
            if st.button("Marcar como resuelto"):
                ts_target = open_inc[idx]["ts"]
                for inc in st.session_state["incidents"]:
                    if inc["ts"] == ts_target:
                        inc["status"] = "Resuelto"
                        inc["close_ts"] = datetime.now().isoformat()
                st.success("Hallazgo cerrado.")
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# 1.2 — INVENTARIO Y MONITOREO DE RECURSOS
# Segun manual PDM 1.2: revisar tableros de CloudWatch / Azure Monitor,
# validar metricas principales, identificar anomalias y reportar backups.
# ══════════════════════════════════════════════════════════════════════════════
def area_inventory() -> None:
    _page_header(
        "1.2 — Inventario y Monitoreo de Recursos",
        "CloudWatch | Azure Monitor — Revision 3 veces al dia: 06:00 / 12:00 / 18:00")

    inv_tab, metrics_tab, alerts_tab, hallazgo_tab = st.tabs([
        "Inventario de recursos",
        "Metricas por recurso",
        "Panel de alertas activas",
        "Registrar hallazgo",
    ])

    if _is_aws():
        _aws_inventory(inv_tab, metrics_tab, alerts_tab, hallazgo_tab)
    else:
        _azure_inventory(inv_tab, metrics_tab, alerts_tab, hallazgo_tab)


# ── AWS ───────────────────────────────────────────────────────────────────────
def _aws_inventory(inv_tab, metrics_tab, alerts_tab, hallazgo_tab) -> None:
    s = _aws()

    with inv_tab:
        st.subheader("Inventario AWS — Region activa: " + _region())
        res_type = st.selectbox("Tipo de recurso",
                                ["EC2", "RDS", "Lambda", "S3", "Load Balancers"],
                                key="aws_inv_type")
        cache_map = {
            "EC2":            (f"aws_inv_ec2_{_region()}", aws.inventory_ec2, s, _region()),
            "RDS":            (f"aws_inv_rds_{_region()}", aws.inventory_rds, s, _region()),
            "Lambda":         (f"aws_inv_lmb_{_region()}", aws.inventory_lambda, s, _region()),
            "S3":             ("aws_inv_s3",               aws.inventory_s3, s),
            "Load Balancers": (f"aws_inv_elb_{_region()}", aws.inventory_elb, s, _region()),
        }
        ck, fn, *args = cache_map[res_type]
        df_inv = cached(ck, fn, *args)

        # Resumen ejecutivo
        if df_inv is not None and not df_inv.empty:
            c1, c2 = st.columns(2)
            c1.metric(f"Total {res_type}", len(df_inv))
            if res_type == "EC2" and "state" in df_inv.columns:
                running = len(df_inv[df_inv["state"] == "running"])
                stopped = len(df_inv[df_inv["state"] == "stopped"])
                c2.metric("Running / Stopped", f"{running} / {stopped}")
            elif res_type == "RDS" and "status" in df_inv.columns:
                avail = len(df_inv[df_inv["status"] == "available"])
                c2.metric("Available", avail)

        show_df(df_inv, f"aws_inv_{res_type.lower()}")

    with metrics_tab:
        st.subheader("Metricas en tiempo real — CloudWatch")
        st.caption("Selecciona un recurso del inventario para ver sus metricas de las ultimas horas.")

        metric_type = st.selectbox("Tipo de recurso",
                                   ["EC2", "RDS", "Lambda"],
                                   key="aws_metric_type")
        hours = st.slider("Ventana de tiempo (horas)", 1, 24, 3, key="aws_metric_hours")

        if metric_type == "EC2":
            df_ec2 = cached(f"aws_inv_ec2_{_region()}", aws.inventory_ec2, s, _region())
            if df_ec2 is not None and not df_ec2.empty:
                sel = st.selectbox(
                    "Instancia EC2",
                    df_ec2["instanceId"].tolist(),
                    format_func=lambda x: (
                        f"{x} ({df_ec2[df_ec2['instanceId']==x]['name'].values[0]})"
                        if not df_ec2[df_ec2["instanceId"]==x].empty else x),
                    key="aws_sel_ec2")
                if sel:
                    df_m = cached(
                        f"aws_met_ec2_{sel}_{hours}",
                        aws.get_ec2_metrics, s, sel, _region(), hours)
                    _render_metrics_chart(df_m, f"EC2: {sel}")
            else:
                st.info("Carga primero el inventario EC2.")

        elif metric_type == "RDS":
            df_rds = cached(f"aws_inv_rds_{_region()}", aws.inventory_rds, s, _region())
            if df_rds is not None and not df_rds.empty:
                sel = st.selectbox("Instancia RDS",
                                   df_rds["dbInstanceId"].tolist(),
                                   key="aws_sel_rds")
                if sel:
                    df_m = cached(
                        f"aws_met_rds_{sel}_{hours}",
                        aws.get_rds_metrics, s, sel, _region(), hours)
                    _render_metrics_chart(df_m, f"RDS: {sel}")
            else:
                st.info("Carga primero el inventario RDS.")

        elif metric_type == "Lambda":
            df_lmb = cached(f"aws_inv_lmb_{_region()}", aws.inventory_lambda, s, _region())
            if df_lmb is not None and not df_lmb.empty:
                sel = st.selectbox("Funcion Lambda",
                                   df_lmb["functionName"].tolist(),
                                   key="aws_sel_lmb")
                if sel:
                    df_m = cached(
                        f"aws_met_lmb_{sel}_{hours}",
                        aws.get_lambda_metrics, s, sel, _region(), hours)
                    _render_metrics_chart(df_m, f"Lambda: {sel}")
            else:
                st.info("Carga primero el inventario Lambda.")

    with alerts_tab:
        _aws_alerts_panel(s)

    with hallazgo_tab:
        _inventory_register_finding("AWS")


def _aws_alerts_panel(s) -> None:
    """
    Panel de alertas CloudWatch segun el manual:
    - Ver todas las alarmas (cualquier estado)
    - Filtrar por estado y namespace
    - Resumen ejecutivo con conteos
    """
    st.subheader("Panel de Alertas — AWS CloudWatch")
    st.caption("Segun el manual PDM: validar comportamiento de metricas "
               "e identificar anomalias activas.")

    df_alarms = cached(f"aws_alarms_all_{_region()}",
                       aws.get_cloudwatch_alarms_all, s, _region())

    if df_alarms is not None and not df_alarms.empty:
        # Resumen ejecutivo
        state_counts = df_alarms.groupby("stateValue").size()
        c1, c2, c3 = st.columns(3)
        c1.metric("Total alarmas",       len(df_alarms))
        c2.metric("En estado ALARM",     int(state_counts.get("ALARM", 0)))
        c3.metric("En estado OK",        int(state_counts.get("OK", 0)))

        alarm_cnt = int(state_counts.get("ALARM", 0))
        if alarm_cnt:
            st.error(f"{alarm_cnt} alarma(s) activa(s) en estado ALARM. Revisar y escalar si aplica.")
        else:
            st.success("Sin alarmas en estado ALARM actualmente.")

        # Filtros
        fc1, fc2 = st.columns(2)
        filter_state = fc1.multiselect(
            "Filtrar por estado",
            df_alarms["stateValue"].unique().tolist(),
            default=["ALARM"] if "ALARM" in df_alarms["stateValue"].values else [],
            key="aws_alarm_state_filter")
        namespaces = ["Todos"] + sorted(df_alarms["namespace"].dropna().unique().tolist())
        filter_ns = fc2.selectbox("Namespace", namespaces, key="aws_alarm_ns")

        df_filtered = df_alarms.copy()
        if filter_state:
            df_filtered = df_filtered[df_filtered["stateValue"].isin(filter_state)]
        if filter_ns != "Todos":
            df_filtered = df_filtered[df_filtered["namespace"] == filter_ns]

        show_df(df_filtered, "aws_alarms_panel")
    else:
        st.info("Sin alarmas disponibles o sin permisos de CloudWatch.")
        export_btns(pd.DataFrame(), "aws_alarms_panel")


# ── Azure ─────────────────────────────────────────────────────────────────────
def _azure_inventory(inv_tab, metrics_tab, alerts_tab, hallazgo_tab) -> None:
    cred  = _cred()
    scope = _scope()

    with inv_tab:
        st.subheader("Inventario Azure — alcance: "
                     f"{len(scope)} suscripcion(es)")
        res_type = st.selectbox(
            "Tipo de recurso",
            ["Maquinas Virtuales (VMs)", "Discos Administrados",
             "Redes Virtuales (VNets)", "Storage Accounts", "AKS Clusters"],
            key="az_inv_type")

        fn_map = {
            "Maquinas Virtuales (VMs)":  azure.inventory_vms,
            "Discos Administrados":      azure.inventory_disks,
            "Redes Virtuales (VNets)":   azure.inventory_vnets,
            "Storage Accounts":          azure.inventory_storage,
            "AKS Clusters":              azure.inventory_aks,
        }
        ck = f"az_inv_{res_type}_{len(scope)}"
        df_inv = cached(ck, _run_all, fn_map[res_type])

        if df_inv is not None and not df_inv.empty:
            c1, c2 = st.columns(2)
            c1.metric(f"Total {res_type}", len(df_inv))
            if res_type == "Maquinas Virtuales (VMs)" and "powerState" in df_inv.columns:
                running = len(df_inv[df_inv["powerState"].str.contains("running", na=False)])
                c2.metric("En ejecucion", running)
            elif res_type == "Discos Administrados" and "diskState" in df_inv.columns:
                unatt = len(df_inv[df_inv["diskState"] == "Unattached"])
                if unatt:
                    c2.metric("Discos no adjuntados", unatt)

        show_df(df_inv, f"az_inv_{res_type[:10].lower()}")

    with metrics_tab:
        st.subheader("Metricas en tiempo real — Azure Monitor")
        st.caption("Selecciona una VM para ver CPU, Red y Disco de las ultimas horas.")

        hours = st.slider("Ventana de tiempo (horas)", 1, 24, 3, key="az_metric_hours")
        ck_vms = f"az_inv_Maquinas Virtuales (VMs)_{len(scope)}"
        df_vms = cached(ck_vms, _run_all, azure.inventory_vms)

        if df_vms is not None and not df_vms.empty:
            # Selector de suscripcion si hay varias
            if "subscriptionName" in df_vms.columns:
                sub_opts = ["Todas"] + df_vms["subscriptionName"].unique().tolist()
                sel_sub = st.selectbox("Suscripcion", sub_opts, key="az_met_sub")
                df_sel = df_vms if sel_sub == "Todas" \
                    else df_vms[df_vms["subscriptionName"] == sel_sub]
            else:
                df_sel = df_vms
                sel_sub = None

            if not df_sel.empty:
                vm_names = df_sel["name"].tolist()
                sel_vm = st.selectbox("Maquina Virtual", vm_names, key="az_sel_vm")
                vm_row = df_sel[df_sel["name"] == sel_vm].iloc[0]

                # Obtener subscription_id de la fila
                sub_id_for_metrics = (vm_row.get("subscriptionId") or
                                      (scope[0]["subscriptionId"] if scope else None))
                resource_id = vm_row.get("id")

                if resource_id and sub_id_for_metrics:
                    df_m = cached(
                        f"az_met_vm_{sel_vm}_{hours}",
                        azure.get_vm_metrics,
                        cred, sub_id_for_metrics, resource_id, hours)
                    _render_metrics_chart(df_m, f"VM: {sel_vm}")
                else:
                    st.warning("No se pudo determinar el ID del recurso para consultar metricas.")
        else:
            st.info("Carga primero el inventario de VMs.")

    with alerts_tab:
        _azure_alerts_panel(cred, scope)

    with hallazgo_tab:
        _inventory_register_finding("Azure")


def _azure_alerts_panel(cred, scope) -> None:
    """
    Panel de alertas Azure Monitor segun el manual:
    - Sev 0, 1, 2 (Critical, Error, Warning)
    - Filtros: suscripcion, severidad, estado, rango de tiempo
    - Resumen ejecutivo con conteos por severidad
    """
    st.subheader("Panel de Alertas — Azure Monitor")
    st.caption(
        "Segun el manual PDM: Monitor -> Alerts -> filtrar Sev 0-2, "
        "Last 24h. Revisar si son Active o Resolved.")

    # Filtros del panel (segun el manual, paso a paso)
    fc1, fc2, fc3 = st.columns(3)
    time_range = fc1.selectbox(
        "Rango de tiempo",
        {"Ultimas 6 horas": 6, "Ultimas 24 horas": 24,
         "Ultimas 48 horas": 48, "Ultimos 7 dias": 168}.keys(),
        index=1, key="az_alert_time")
    time_map = {"Ultimas 6 horas": 6, "Ultimas 24 horas": 24,
                "Ultimas 48 horas": 48, "Ultimos 7 dias": 168}
    hours_sel = time_map[time_range]

    # Barrido multi-suscripcion de alertas
    ck = f"az_alerts_detail_{len(scope)}_{hours_sel}"
    df_alerts = cached(ck, _run_all, azure.get_all_alerts_detail, hours_sel)

    if df_alerts is not None and not df_alerts.empty:
        # Resumen ejecutivo por severidad
        sev_counts = df_alerts.groupby("severity").size()
        m_cols = st.columns(4)
        m_cols[0].metric("Total alertas", len(df_alerts))
        m_cols[1].metric("Critica (Sev 0)", int(sev_counts.get("Sev0", 0)))
        m_cols[2].metric("Error (Sev 1)",   int(sev_counts.get("Sev1", 0)))
        m_cols[3].metric("Advertencia (Sev 2)", int(sev_counts.get("Sev2", 0)))

        crit = int(sev_counts.get("Sev0", 0))
        if crit:
            st.error(f"{crit} alerta(s) CRITICA(S) activas. "
                     "Escalar a Nivel 2 segun criterios del manual.")
        elif len(df_alerts) > 0:
            st.warning(f"{len(df_alerts)} alerta(s) activas en Sev 0-2. Revisar.")
        else:
            st.success("Sin alertas activas en Sev 0-2.")

        # Filtros adicionales
        ff1, ff2 = st.columns(2)
        sevs_available = df_alerts["severity"].unique().tolist()
        filter_sev = ff1.multiselect(
            "Severidad",
            sevs_available,
            default=sevs_available,
            key="az_alert_sev_filter",
            format_func=lambda x: {
                "Sev0": "Sev 0 - Critical",
                "Sev1": "Sev 1 - Error",
                "Sev2": "Sev 2 - Warning"}.get(x, x))
        states = ["Todos"] + df_alerts["alertState"].dropna().unique().tolist()
        filter_state = ff2.selectbox("Estado", states, key="az_alert_state_filter")

        df_f = df_alerts.copy()
        if filter_sev:
            df_f = df_f[df_f["severity"].isin(filter_sev)]
        if filter_state != "Todos":
            df_f = df_f[df_f["alertState"] == filter_state]

        # Tabla con columnas del manual: severidad, estado, recurso, tipo, tiempo
        show_cols = [c for c in ["subscriptionName", "severity", "severityLabel",
                                  "alertState", "monitorCondition", "targetResource",
                                  "targetResourceType", "signalType", "firedTime"]
                     if c in df_f.columns]
        show_df(df_f[show_cols], "az_alerts_panel")

        # Distribucion por severidad (grafico)
        if _PLOTLY and not df_f.empty and "severity" in df_f.columns:
            sev_agg = (df_f.groupby(["severity","severityLabel"])
                          .size().reset_index(name="count"))
            color_map = {"Sev0": "#c0392b", "Sev1": "#e67e22", "Sev2": "#f1c40f"}
            fig = px.bar(sev_agg, x="severityLabel", y="count",
                         title="Distribucion de alertas por severidad",
                         color="severity",
                         color_discrete_map=color_map)
            fig.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                              showlegend=False, height=300)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.success("Sin alertas activas en Sev 0-2 para el periodo seleccionado.")
        export_btns(pd.DataFrame(), "az_alerts_panel")


# ── Grafico de metricas (compartido AWS/Azure) ────────────────────────────────
def _render_metrics_chart(df: pd.DataFrame, title: str) -> None:
    """Renderiza las metricas de un recurso en graficos de linea por metrica."""
    if df is None or df.empty:
        note = getattr(df, "attrs", {}).get("note") if df is not None else ""
        st.warning(note or "Sin datos de metricas disponibles para este recurso.")
        return

    metrics = df["metric"].unique().tolist()
    st.caption(f"{len(metrics)} metricas disponibles para: {title}")

    if _PLOTLY:
        for metric in metrics:
            df_m = df[df["metric"] == metric].copy()
            if df_m.empty:
                continue
            unit = df_m["value"].name if "unit" not in df_m.columns else df_m["unit"].iloc[0]
            fig = px.line(
                df_m, x="timestamp", y="value",
                title=f"{metric} ({unit})",
                labels={"timestamp": "Hora (UTC)", "value": metric},
                color_discrete_sequence=["#0050A0"])
            fig.update_layout(
                height=220, plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(t=40, b=20), xaxis_tickangle=-30)
            fig.update_traces(line_width=2)
            st.plotly_chart(fig, use_container_width=True)
    else:
        for metric in metrics:
            df_m = df[df["metric"] == metric].set_index("timestamp")["value"]
            st.line_chart(df_m)
            st.caption(metric)

    export_btns(df, f"metrics_{title[:20].replace(' ','_')}")


# ── Registro de hallazgos del inventario ─────────────────────────────────────
def _inventory_register_finding(cloud: str) -> None:
    """
    Segun el manual PDM: registrar hallazgos con fecha, servicio,
    region, estado, observaciones y escalar si aplica.
    """
    st.subheader("Registrar hallazgo de monitoreo")
    st.caption("Segun el manual: documenta fecha, hora, recurso, "
               "metrica, valor, observaciones y accion tomada.")

    with st.form(f"inv_finding_{cloud}"):
        c1, c2, c3 = st.columns(3)
        recurso  = c1.text_input("Recurso / Servicio afectado")
        region   = c2.text_input("Region / Suscripcion")
        metrica  = c3.text_input("Metrica (ej. CPU, Memoria, Disco)")
        c4, c5 = st.columns(2)
        valor    = c4.text_input("Valor observado")
        umbral   = c5.text_input("Umbral / valor esperado")
        estado   = st.selectbox("Estado detectado",
                                ["Anomalia activa", "Alerta transitoria",
                                 "Falso positivo", "En observacion", "Resuelto"])
        obs      = st.text_area("Observaciones y accion tomada")
        escalate = st.checkbox("Escalar a Nivel 2")
        submitted = st.form_submit_button("Registrar hallazgo")
        if submitted and recurso:
            st.session_state["incidents"].append({
                "ts":       datetime.now().isoformat(),
                "provider": cloud,
                "region":   region,
                "service":  recurso,
                "metrica":  metrica,
                "valor":    valor,
                "umbral":   umbral,
                "estado":   estado,
                "obs":      obs,
                "escalado": escalate,
                "status":   "Abierto",
                "close_ts": None,
            })
            msg = "Hallazgo registrado."
            if escalate:
                msg += " Marcado para escalamiento a Nivel 2."
            st.success(msg)

    # Tabla de hallazgos previos
    cloud_findings = [i for i in st.session_state["incidents"]
                      if i.get("provider") == cloud]
    if cloud_findings:
        st.subheader("Hallazgos registrados")
        df_f = pd.DataFrame(cloud_findings)
        st.dataframe(df_f, use_container_width=True, hide_index=True)
        export_btns(df_f, f"inv_findings_{cloud.lower()}")

        open_f = [i for i in cloud_findings if i["status"] == "Abierto"]
        if open_f:
            idx = st.selectbox(
                "Cerrar hallazgo", range(len(open_f)),
                format_func=lambda i:
                f"{open_f[i]['ts'][:16]} | {open_f[i]['service']} | {open_f[i]['estado']}",
                key=f"close_inv_{cloud}")
            if st.button("Marcar como resuelto", key=f"resolve_inv_{cloud}"):
                ts_t = open_f[idx]["ts"]
                for inc in st.session_state["incidents"]:
                    if inc["ts"] == ts_t:
                        inc["status"]   = "Resuelto"
                        inc["close_ts"] = datetime.now().isoformat()
                st.success("Hallazgo cerrado.")
                st.rerun()
    else:
        st.info("Sin hallazgos registrados para esta nube.")


def _aws_fin_action(s, row) -> dict:
    t = row["type"]
    if t.startswith("EBS"):     return aws.delete_ebs_volume(s, row["resourceId"], _region())
    if t.startswith("Elastic"): return aws.release_eip(s, row["resourceId"], _region())
    if t.startswith("LB"):      return aws.delete_lb(s, row["extra"], _region())
    return {"ok": False, "message": "Tipo no soportado."}


def _azure_fin_action(cred, sub_id, row) -> dict:
    t, rid = row["type"], row["resourceId"]
    if t.startswith("ManagedDisk"):  return azure.delete_disk(cred, sub_id, rid)
    if t.startswith("PublicIP"):     return azure.delete_public_ip(cred, sub_id, rid)
    if t.startswith("LoadBalancer"): return azure.delete_lb(cred, sub_id, rid)
    return {"ok": False, "message": "Tipo no soportado."}


# ══════════════════════════════════════════════════════════════════════════════
# 1.3 — SEGURIDAD Y CUMPLIMIENTO
# ══════════════════════════════════════════════════════════════════════════════
def area_security() -> None:
    _page_header("1.3 — Seguridad y Cumplimiento")
    mon_tab, action_tab, strat_tab = st.tabs(
        ["Monitoreo L1", "Remediacion L2", "Estrategia L3"])

    if _is_aws():
        s = _aws()
        with mon_tab:
            st.subheader("Security Hub — CRITICAL / HIGH activos")
            df_sec = cached("aws_sechub", aws.security_findings, s)
            if df_sec is not None and not df_sec.empty:
                crit = (df_sec[df_sec["severity"] == "CRITICAL"]
                        if "severity" in df_sec.columns else pd.DataFrame())
                if not crit.empty:
                    st.error(f"{len(crit)} hallazgo(s) CRITICAL sin resolver.")
            show_df(df_sec, "aws_sechub")
            st.subheader("CloudWatch — Alarmas en estado ALARM")
            show_df(cached(f"aws_cw_{_region()}", aws.cloudwatch_alarms, s, _region()), "aws_cw")
            st.subheader("SSM Patch Manager — Estado de parches")
            show_df(cached(f"aws_patch_{_region()}", aws.patch_compliance, s, _region()),
                    "aws_patch")

        with action_tab:
            st.subheader("Resolver hallazgo de Security Hub")
            df_sec = cached("aws_sechub", aws.security_findings, s)
            if df_sec is not None and not df_sec.empty:
                idx = st.selectbox("Hallazgo", df_sec.index,
                                   format_func=lambda i:
                                   f"{df_sec.loc[i,'severity']} — {df_sec.loc[i,'title']}")
                new_status  = st.selectbox("Nuevo estado",
                                           ["RESOLVED", "NOTIFIED", "SUPPRESSED"])
                product_arn = st.text_input("Product ARN del hallazgo")
                if st.checkbox("Confirmo la remediacion", key="aws_sec_confirm"):
                    if st.button("Aplicar"):
                        action_result(aws.resolve_finding(
                            s, df_sec.loc[idx, "findingId"], product_arn, new_status))
            else:
                st.success("Sin hallazgos activos.")

            st.subheader("Parchear instancias Non-Compliant")
            df_p = cached(f"aws_patch_{_region()}", aws.patch_compliance, s, _region())
            if df_p is not None and not df_p.empty:
                nc = (df_p[df_p["complianceStatus"] == "NON_COMPLIANT"]["instanceId"]
                      .tolist())
                if nc:
                    sel = st.multiselect("Instancias a parchear", nc)
                    if sel and st.checkbox("Confirmo el parcheo", key="aws_patch_confirm"):
                        if st.button("Instalar parches"):
                            action_result(aws.run_patch_baseline(s, sel, _region()))
                else:
                    st.success("Todas las instancias en compliance.")

        with strat_tab:
            st.subheader("Well-Architected — Estandares de Security Hub")
            show_df(cached("aws_waf", aws.well_architected_standards, s), "aws_waf")

    else:
        with mon_tab:
            st.subheader("Defender for Cloud — Secure Score")
            df_ss = cached("az_score", _run_all, azure.secure_score)
            show_df(df_ss, "az_score")
            _gauge_score(df_ss)
            st.subheader("Defender for Cloud — Alertas Critical / High")
            df_sec = cached("az_sec", _run_all, azure.security_alerts)
            if df_sec is not None and not df_sec.empty:
                st.error(f"{len(df_sec)} alerta(s) activas de seguridad.")
            show_df(df_sec, "az_sec")
            st.subheader("Actualizaciones disponibles — Azure Advisor")
            show_df(cached("az_upd", _run_all, azure.available_updates), "az_upd")

        with action_tab:
            st.subheader("Descartar alerta de Defender for Cloud")
            df_sec = cached("az_sec", _run_all, azure.security_alerts)
            if df_sec is not None and not df_sec.empty:
                idx = st.selectbox("Alerta", df_sec.index,
                                   format_func=lambda i:
                                   f"[{df_sec.loc[i,'subscriptionName']}] "
                                   f"{df_sec.loc[i,'severity']} — "
                                   f"{df_sec.loc[i,'alertDisplayName']}")
                row = df_sec.loc[idx]
                loc = st.text_input("ASC location", value="centralus")
                if st.checkbox("Confirmo descartar la alerta", key="az_sec_confirm"):
                    if st.button("Descartar"):
                        action_result(azure.dismiss_security_alert(
                            _cred(), row["subscriptionId"], row["alertName"], loc))
            else:
                st.success("Sin alertas activas.")

        with strat_tab:
            st.subheader("Well-Architected — Controles de Defender for Cloud")
            show_df(cached("az_waf", _run_all, azure.well_architected_controls), "az_waf")


def _gauge_score(df) -> None:
    if df is None or df.empty or "percentage" not in df.columns or not _PLOTLY:
        return
    avg = df["percentage"].dropna().mean()
    if pd.isna(avg):
        return
    color = "#1a7a4a" if avg >= 70 else "#c87800" if avg >= 50 else "#c0392b"
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=avg,
        title={"text": "Secure Score promedio (%)"},
        gauge={"axis": {"range": [0, 100]}, "bar": {"color": color},
               "steps": [{"range": [0, 50], "color": "#fee0d2"},
                          {"range": [50, 70], "color": "#fff3cd"},
                          {"range": [70, 100], "color": "#d4edda"}]}))
    fig.update_layout(height=280, paper_bgcolor="white", plot_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# 1.4 — CICLO DE VIDA
# ══════════════════════════════════════════════════════════════════════════════
def area_lifecycle() -> None:
    _page_header("1.4 — Ciclo de Vida — Servicios Deprecados")
    mon_tab, plan_tab = st.tabs(["Monitoreo L1", "Plan de Migracion L2/L3"])

    if _is_aws():
        s = _aws()
        with mon_tab:
            st.subheader("AWS Health — Eventos de retiro / deprecacion")
            df = cached("aws_depr", aws.deprecated_services, s)
            if df is not None and not df.empty:
                st.warning(f"{len(df)} servicio(s) con avisos de retiro proximos.")
            show_df(df, "aws_depr")
    else:
        with mon_tab:
            st.subheader("Azure Advisor — Retiros / Deprecaciones")
            df = cached("az_depr", _run_all, azure.deprecated_services)
            if df is not None and not df.empty:
                st.warning(f"{len(df)} servicio(s) con avisos de retiro.")
            show_df(df, "az_depr")

    with plan_tab:
        _lifecycle_plan()


def _lifecycle_plan() -> None:
    st.subheader("Gestion de planes de migracion")
    cloud_key = "AWS" if _is_aws() else "Azure"
    df = st.session_state["cache"].get("aws_depr" if _is_aws() else "az_depr")
    service_opts = []
    if df is not None and not df.empty:
        col_svc = "service" if "service" in df.columns else "problem"
        service_opts = df[col_svc].dropna().unique().tolist()

    with st.form("lifecycle_form"):
        c1, c2 = st.columns(2)
        svc     = (c1.selectbox("Servicio obsoleto", service_opts)
                   if service_opts else c1.text_input("Servicio"))
        owner   = c2.text_input("Responsable")
        plan    = st.text_area("Plan de migracion detallado")
        deadline = st.date_input("Fecha limite")
        priority = st.select_slider("Prioridad", ["Baja", "Media", "Alta", "Critica"])
        if st.form_submit_button("Guardar plan"):
            st.session_state["lifecycle"][f"{cloud_key}_{svc}"] = {
                "cloud": cloud_key, "service": svc, "owner": owner,
                "plan": plan, "deadline": str(deadline),
                "priority": priority, "ts": datetime.now().isoformat()}
            st.success("Plan guardado.")

    plans = list(st.session_state["lifecycle"].values())
    if plans:
        df_p = pd.DataFrame(plans)
        st.dataframe(df_p, use_container_width=True, hide_index=True)
        export_btns(df_p, "migration_plans")
    else:
        st.info("Sin planes registrados.")


# ══════════════════════════════════════════════════════════════════════════════
# CENTRO DE MANDO KPIs
# ══════════════════════════════════════════════════════════════════════════════
def area_kpis() -> None:
    _page_header("Centro de Mando KPIs",
                 "Metricas operativas del Plan de Administracion Cloud")
    form_tab, dash_tab = st.tabs(["Registro de metricas", "Tablero de KPIs"])

    with form_tab:
        d = st.session_state["kpi_data"]
        st.subheader("L1 — Calidad y Velocidad")
        c = st.columns(4)
        d["total_alerts"]     = c[0].number_input("Alertas totales",     0, value=d.get("total_alerts", 0),     key="k_ta")
        d["ack_count"]        = c[1].number_input("Reconocidas",         0, value=d.get("ack_count", 0),        key="k_ac")
        d["sum_ack_min"]      = c[2].number_input("Sum min reconoc.",    0.0, value=d.get("sum_ack_min", 0.0),  key="k_am")
        d["proactive_alerts"] = c[3].number_input("Proactivas",          0, value=d.get("proactive_alerts", 0), key="k_pa")
        c2 = st.columns(4)
        d["resolved_count"]   = c2[0].number_input("Incidentes resueltos",0, value=d.get("resolved_count", 0),  key="k_rc")
        d["sum_res_min"]      = c2[1].number_input("Sum min resolucion", 0.0, value=d.get("sum_res_min", 0.0),  key="k_rm")
        d["fp_count"]         = c2[2].number_input("Falsos positivos",   0, value=d.get("fp_count", 0),         key="k_fp")

        st.subheader("L2 — Gestion Operativa")
        c3 = st.columns(3)
        d["esc_ok"]           = c3[0].number_input("Escalamientos sin error", 0, value=d.get("esc_ok", 0),      key="k_eo")
        d["esc_total"]        = c3[1].number_input("Escalamientos totales",   0, value=d.get("esc_total", 0),   key="k_et")
        d["chg_ok"]           = c3[2].number_input("Cambios conformes",       0, value=d.get("chg_ok", 0),      key="k_co")
        c4 = st.columns(3)
        d["chg_total"]        = c4[0].number_input("Cambios totales",    0, value=d.get("chg_total", 0),        key="k_ct")
        d["cost_baseline"]    = c4[1].number_input("Costo baseline ($)", 0.0, value=d.get("cost_baseline", 0.0),key="k_cb")
        d["cost_current"]     = c4[2].number_input("Costo actual ($)",   0.0, value=d.get("cost_current", 0.0), key="k_cc")

        st.subheader("L3 — Arquitectura y Automatizacion")
        c5 = st.columns(3)
        d["auto_done"]        = c5[0].number_input("Automatizaciones hechas",      0, value=d.get("auto_done", 0),    key="k_ad")
        d["auto_planned"]     = c5[1].number_input("Automatizaciones planificadas",0, value=d.get("auto_planned", 0), key="k_ap")
        d["proj_ok"]          = c5[2].number_input("Proyectos sin incidentes",     0, value=d.get("proj_ok", 0),      key="k_po")
        c6 = st.columns(3)
        d["proj_total"]       = c6[0].number_input("Proyectos totales",    0, value=d.get("proj_total", 0),      key="k_pt")
        d["perf_before"]      = c6[1].number_input("Rendimiento antes",   0.0, value=d.get("perf_before", 0.0), key="k_pb")
        d["perf_after"]       = c6[2].number_input("Rendimiento despues", 0.0, value=d.get("perf_after", 0.0),  key="k_pf")
        st.session_state["kpi_data"] = d

    with dash_tab:
        kpis = kpm.build_kpis(st.session_state["kpi_data"])
        global_st = kpm.summary_status(kpis)
        st.subheader(f"Estado global del periodo: {global_st}")
        st.markdown("---")

        cols1 = st.columns(5)
        for col, k in zip(cols1, kpis[:5]):
            with col:
                val = "—" if k.value is None else f"{k.value} {k.unit}"
                delta_txt = f"{'<=' if k.direction=='lte' else '>='} {k.target}{k.unit}"
                st.metric(k.label, val, delta=delta_txt, delta_color="off")
        cols2 = st.columns(5)
        for col, k in zip(cols2, kpis[5:]):
            with col:
                val = "—" if k.value is None else f"{k.value} {k.unit}"
                delta_txt = f"{'<=' if k.direction=='lte' else '>='} {k.target}{k.unit}"
                st.metric(k.label, val, delta=delta_txt, delta_color="off")

        if _PLOTLY and kpis:
            color_map = {"green": "#1a7a4a", "orange": "#c87800",
                         "red": "#c0392b", "gray": "#aaaaaa"}
            labels  = [k.label for k in kpis]
            values  = [k.value if k.value is not None else 0 for k in kpis]
            targets = [k.target for k in kpis]
            colors  = [color_map.get(k.color, "#aaaaaa") for k in kpis]
            fig = go.Figure()
            fig.add_bar(name="Valor actual", x=labels, y=values, marker_color=colors)
            fig.add_scatter(name="Meta", x=labels, y=targets, mode="markers",
                            marker=dict(symbol="line-ew-open", size=30,
                                        color="#333", line_width=3))
            fig.update_layout(height=380, title="KPIs — Valor actual vs Meta",
                              xaxis_tickangle=-30,
                              plot_bgcolor="white", paper_bgcolor="white",
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, use_container_width=True)

        df_k = pd.DataFrame([k.to_dict() for k in kpis])
        st.dataframe(df_k, use_container_width=True, hide_index=True)
        export_btns(df_k, "kpis")

        resolved = [i for i in st.session_state["incidents"]
                    if i.get("status") == "Resuelto" and i.get("close_ts")]
        if resolved and _PLOTLY:
            st.subheader("MTTR por hallazgo registrado (minutos)")
            rows = []
            for inc in resolved:
                try:
                    mins = (datetime.fromisoformat(inc["close_ts"]) -
                            datetime.fromisoformat(inc["ts"])).total_seconds() / 60
                    rows.append({
                        "Hallazgo": f"{inc.get('provider','')} | {inc.get('service','')}",
                        "MTTR (min)": round(mins, 1)})
                except Exception:  # noqa: BLE001
                    pass
            if rows:
                df_m = pd.DataFrame(rows)
                fig2 = px.bar(df_m, x="Hallazgo", y="MTTR (min)",
                              title="MTTR por hallazgo registrado",
                              color="MTTR (min)",
                              color_continuous_scale=["#1a7a4a", "#c87800", "#c0392b"])
                fig2.add_hline(y=120, line_dash="dash", line_color="#c0392b",
                               annotation_text="Meta: 120 min")
                fig2.update_layout(plot_bgcolor="white", paper_bgcolor="white")
                st.plotly_chart(fig2, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
_DISPATCH = {
    AREAS[0]: area_cloud_health,
    AREAS[1]: area_inventory,
    AREAS[2]: area_security,
    AREAS[3]: area_lifecycle,
    AREAS[4]: area_kpis,
}


def render_main() -> None:
    area = render_sidebar()
    cloud = st.session_state.get("active_cloud", "—")
    st.caption(f"Nube activa: {cloud}")
    try:
        _DISPATCH[area]()
    except aws.AWSAuthError as e:
        auth_err(str(e))
    except azure.AzureAuthError as e:
        auth_err(str(e))
    except Exception as e:  # noqa: BLE001
        st.error(f"Error inesperado: {e}")


def main() -> None:
    if not st.session_state.get("auth_done"):
        render_login()
    else:
        render_main()


if __name__ == "__main__":
    main()
