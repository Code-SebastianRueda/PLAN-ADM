"""
app.py  —  PLAN-ADM | Cloud Operations Dashboard (Login multicloud)
===================================================================
Pantalla de inicio del Plan de Administracion Operativa Cloud.

Diseño: Cloud Operations Dashboard empresarial con identidad Kyndryl
(paleta azul / blanco / gris). Ambiente UNICO: commercial.

Estructura (de arriba hacia abajo):
    1. Cabecera corporativa (logo Kyndryl grande + titulo + ambiente fijo).
    2. Resumen global de disponibilidad + accion de actualizacion.
    3. Tarjetas de estado por nube (AWS, Azure, GCP, OCI, IBM Cloud) con
       logo del proveedor, badge de estado y enlace a la pagina oficial.
    4. Resumen de incidentes activos (expandible).
    5. Conexiones Cloud (login por proveedor en tabs).

La logica de negocio (consulta de estado en cloud_status.py) no cambia.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import cloud_status as cstatus
import azure_auth
import aws_auth
import session_store
import azure_alerts
import azure_deprecations
import azure_inventory
import azure_security

try:
    import plotly.express as px
    _PLOTLY = True
except Exception:  # noqa: BLE001
    _PLOTLY = False

# ===========================================================================
# Configuracion de pagina
# ===========================================================================
_LOGO_PATH = Path(__file__).parent / "Logo.svg"
_ENVIRONMENT = "commercial"          # Ambiente UNICO. No hay seleccion de ambiente.
_PROVIDERS = ["AWS", "Azure", "GCP", "OCI", "IBM Cloud"]

# Portales AWS IAM Identity Center por cliente.
# Cadena vacia = portal aun no configurado (se agregara su enlace mas adelante).
_AWS_CLIENTS = {
    "Bolivar":   "https://solucionesbolivar.awsapps.com/start/#/?tab=accounts",
    "Coomeva":   "",
    "Colombina": "",
}

st.set_page_config(
    page_title="PLAN-ADM | Cloud Operations",
    page_icon=str(_LOGO_PATH) if _LOGO_PATH.exists() else "☁️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Paleta corporativa y estados
# ---------------------------------------------------------------------------
_KYNDRYL_BLUE = "#0050A0"
_KYNDRYL_BLUE_DARK = "#003A75"

_CLOUD_BRAND = {
    "AWS":       "#FF9900",
    "Azure":     "#0078D4",
    "GCP":       "#4285F4",
    "OCI":       "#C74634",
    "IBM Cloud": "#0F62FE",
}

# Indicador normalizado -> (emoji, etiqueta, color de badge, fondo del badge)
_STATE = {
    "ok":       ("🟢", "Operativo",            "#1a7a4a", "#e6f4ea"),
    "minor":    ("🟡", "Degradacion parcial",  "#b7791f", "#fff8e1"),
    "major":    ("🟠", "Intermitencia",        "#c05621", "#fff1e6"),
    "critical": ("🔴", "Incidente",            "#c0392b", "#fdecea"),
    "unknown":  ("⚪", "Sin informacion",      "#546e7a", "#eceff1"),
}

# ---------------------------------------------------------------------------
# Logos oficiales de cada proveedor (SVG descargados en assets/)
# ---------------------------------------------------------------------------
_ASSETS = Path(__file__).parent / "assets"
_LOGO_FILES = {
    "AWS":       "aws.svg",
    "Azure":     "azure.svg",
    "GCP":       "gcp.svg",
    "OCI":       "oci.svg",
    "IBM Cloud": "ibm.svg",
}


@st.cache_data(show_spinner=False)
def _logo_svg(provider: str) -> str:
    """Devuelve el markup del logo oficial del proveedor (SVG local).

    Si el archivo no existe, cae a un texto simple con el color de marca para
    no romper el render.
    """
    fname = _LOGO_FILES.get(provider)
    if fname:
        path = _ASSETS / fname
        try:
            svg = path.read_text(encoding="utf-8")
            # Normaliza: quita width/height fijos para que escale al contenedor.
            svg = re.sub(r'(<svg[^>]*?)\swidth="[^"]*"', r"\1", svg, count=1)
            svg = re.sub(r'(<svg[^>]*?)\sheight="[^"]*"', r"\1", svg, count=1)
            return svg
        except Exception:  # noqa: BLE001
            pass
    b = _CLOUD_BRAND.get(provider, _KYNDRYL_BLUE)
    return (f"<span style='font-weight:800;font-size:1.1rem;color:{b};'>"
            f"{provider}</span>")


# ===========================================================================
# CSS corporativo (moderno, responsive, sombras sutiles, hover)
# ===========================================================================
st.markdown(
    f"""
    <style>
    :root {{
        --kblue: {_KYNDRYL_BLUE};
        --kblue-dark: {_KYNDRYL_BLUE_DARK};
        --ink: #1A2230;
        --muted: #5b6472;
        --line: #e3e8ef;
        --surface: #ffffff;
        --surface-2: #f5f7fa;
    }}
    html, body, [class*="css"] {{
        font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
        background: var(--surface-2);
        color: var(--ink);
    }}
    .block-container {{ padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1500px; }}

    /* ---------- Header ---------- */
    .cops-header {{
        display: flex; align-items: center; gap: 1.1rem;
        background: linear-gradient(100deg, #ffffff 0%, #f3f7fc 100%);
        border: 1px solid var(--line);
        border-left: 6px solid var(--kblue);
        border-radius: 14px;
        padding: 1.1rem 1.4rem;
        box-shadow: 0 2px 10px rgba(16,42,80,0.05);
        margin-bottom: 1.3rem;
    }}
    .cops-header .logo img {{ height: 64px; width: auto; }}
    .cops-header .titles h1 {{
        font-size: 1.7rem; font-weight: 800; color: var(--kblue);
        margin: 0; line-height: 1.15; letter-spacing: -0.3px;
    }}
    .cops-header .titles .sub {{
        font-size: 0.95rem; color: var(--muted); margin-top: 2px; font-weight: 500;
    }}
    .cops-header .env {{
        margin-left: auto; align-self: flex-start;
        background: var(--surface-2); border: 1px solid var(--line);
        color: var(--muted); font-size: 0.74rem; font-weight: 700;
        letter-spacing: .4px; text-transform: uppercase;
        padding: 5px 11px; border-radius: 999px; white-space: nowrap;
    }}
    .cops-header .env b {{ color: var(--kblue); }}

    /* ---------- Section titles ---------- */
    .cops-section {{
        display:flex; align-items:center; gap:.55rem;
        font-size: 1.12rem; font-weight: 750; color: var(--ink);
        margin: .3rem 0 .7rem 0;
    }}
    .cops-section .bar {{ width: 4px; height: 20px; border-radius: 3px; background: var(--kblue); }}

    /* ---------- Global availability banner ---------- */
    .cops-overview {{
        display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
        background: var(--surface); border:1px solid var(--line);
        border-radius: 12px; padding: .85rem 1.1rem; margin-bottom: 1rem;
        box-shadow: 0 1px 6px rgba(16,42,80,0.04);
    }}
    .cops-overview .big {{ font-size: 1.5rem; font-weight: 800; }}
    .cops-overview .lbl {{ color: var(--muted); font-size: .9rem; font-weight:600; }}
    .cops-overview .pill {{
        padding: 4px 12px; border-radius: 999px; font-weight: 700; font-size: .82rem;
    }}

    /* ---------- Cloud status cards ---------- */
    .cloud-grid {{
        display: grid; gap: 14px;
        grid-template-columns: repeat(5, 1fr);
    }}
    @media (max-width: 1300px) {{ .cloud-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
    @media (max-width: 900px)  {{ .cloud-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    @media (max-width: 560px)  {{ .cloud-grid {{ grid-template-columns: 1fr; }} }}

    .ccard {{
        background: var(--surface); border: 1px solid var(--line);
        border-top: 4px solid var(--brand); border-radius: 14px;
        padding: 16px 15px 14px 15px; display:flex; flex-direction:column;
        gap: 8px; min-height: 212px;
        box-shadow: 0 2px 8px rgba(16,42,80,0.05);
        transition: transform .16s ease, box-shadow .16s ease;
    }}
    .ccard:hover {{ transform: translateY(-3px); box-shadow: 0 8px 20px rgba(16,42,80,0.12); }}
    .ccard .logo {{
        height: 58px; display:flex; align-items:center; justify-content:center;
        background: var(--surface-2); border-radius: 10px; padding: 10px 12px;
    }}
    .ccard .logo svg {{ max-height: 40px; max-width: 100%; width:auto; height:auto; }}
    .ccard .pname {{ font-size: 1.02rem; font-weight: 800; color: var(--ink); }}
    .ccard .badge {{
        display:inline-flex; align-items:center; gap:6px; align-self:flex-start;
        padding: 5px 11px; border-radius: 999px; font-weight: 750; font-size: .82rem;
        color: var(--bcolor); background: var(--bbg); border:1px solid rgba(0,0,0,.04);
    }}
    .ccard .badge .dot {{ font-size: .9rem; line-height: 1; }}
    .ccard .desc {{ font-size: .8rem; color: var(--muted); min-height: 2.2em; line-height:1.25; }}
    .ccard .regions {{ font-size: .72rem; color: #8894a6; }}
    .ccard .official {{
        margin-top: auto; display:inline-flex; align-items:center; gap:6px;
        font-size: .8rem; font-weight: 700; color: var(--kblue);
        text-decoration: none; padding-top: 8px; border-top: 1px dashed var(--line);
    }}
    .ccard .official:hover {{ color: var(--kblue-dark); text-decoration: underline; }}

    /* ---------- Buttons ---------- */
    [data-testid="stButton"] > button[kind="primary"] {{
        background: var(--kblue); color: #fff; border: none; border-radius: 9px;
        font-weight: 700; padding: .5rem 1rem;
        transition: background .15s ease, box-shadow .15s ease;
    }}
    [data-testid="stButton"] > button[kind="primary"]:hover {{
        background: var(--kblue-dark); box-shadow: 0 4px 12px rgba(0,80,160,.28);
    }}
    [data-testid="stButton"] > button[kind="secondary"] {{
        background: #fff; color: var(--kblue); border: 1.5px solid var(--kblue);
        border-radius: 9px; font-weight: 700;
    }}
    [data-testid="stButton"] > button[kind="secondary"]:hover {{
        background: #eef4fb;
    }}

    /* ---------- Tabs ---------- */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: 4px; }}
    [data-testid="stTabs"] button {{ font-weight: 700; color: var(--muted); }}
    [data-testid="stTabs"] button[aria-selected="true"] {{ color: var(--kblue); }}

    /* ---------- Expander ---------- */
    [data-testid="stExpander"] {{ border-radius: 12px; border:1px solid var(--line); }}
    [data-testid="stExpander"] summary {{ font-weight: 700; color: var(--ink); }}

    /* ---------- Login provider card ---------- */
    .login-head {{
        display:flex; align-items:center; gap:12px; margin-bottom: .3rem;
    }}
    .login-head .lg {{
        height: 46px; width: 78px; display:flex; align-items:center; justify-content:center;
        background: var(--surface-2); border-radius: 10px; padding: 6px 10px;
    }}
    .login-head .lg svg {{ max-height: 32px; max-width: 100%; width:auto; height:auto; }}
    .login-head .nm {{ font-size: 1.08rem; font-weight: 800; color: var(--ink); }}
    .login-head .mt {{ font-size: .8rem; color: var(--muted); }}
    .conn-badge {{
        display:inline-flex; align-items:center; gap:6px; padding:3px 10px;
        border-radius:999px; font-size:.75rem; font-weight:700;
    }}
    .conn-on  {{ background:#e6f4ea; color:#1a7a4a; }}
    .conn-off {{ background:#eceff1; color:#5b6472; }}

    /* ---------- AWS approve link-button ---------- */
    .aws-portal-btn {{
        display:flex; align-items:center; justify-content:center; gap:8px;
        width:100%; box-sizing:border-box;
        background:#FF9900; color:#232F3E !important; font-weight:800;
        text-decoration:none; padding:.6rem 1rem; border-radius:9px;
        border:1px solid #e88b00; margin:.3rem 0 .5rem;
        transition: background .15s ease, box-shadow .15s ease;
    }}
    .aws-portal-btn:hover {{
        background:#f0920a; box-shadow:0 4px 12px rgba(255,153,0,.35);
        text-decoration:none;
    }}

    /* ---------- Sidebar (panel central) ---------- */
    section[data-testid="stSidebar"] {{
        background: #ffffff; border-right: 1px solid var(--line);
    }}
    .sb-title {{
        font-size: .72rem; font-weight: 800; letter-spacing: .5px;
        text-transform: uppercase; color: #8894a6; margin: .2rem 0 .4rem;
    }}
    .sb-cloud {{
        display:flex; align-items:center; gap:8px;
        font-weight: 700; font-size: .9rem; color: var(--ink);
        padding: 5px 10px; border-radius: 8px; background: var(--surface-2);
        margin-bottom: 5px;
    }}
    .sb-dot {{
        width: 8px; height: 8px; border-radius: 50%;
        background: #1a7a4a; box-shadow: 0 0 0 3px rgba(26,122,74,.15);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ===========================================================================
# Cabecera
# ===========================================================================
def render_header() -> None:
    logo_html = (
        f"<div class='logo'><img src='data:image/svg+xml;base64,{_logo_b64()}'/></div>"
        if _LOGO_PATH.exists() else "<div class='logo'></div>"
    )
    st.markdown(
        f"""
        <div class='cops-header'>
            {logo_html}
            <div class='titles'>
                <h1>☁️ Plan de Administracion Operativa Cloud</h1>
                <div class='sub'>Cloud Operations Dashboard · Estado e inicio de sesion multicloud</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _logo_b64() -> str:
    import base64
    try:
        return base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        return ""


# ===========================================================================
# Datos de estado (cacheado)
# ===========================================================================
@st.cache_data(ttl=120, show_spinner=False)
def _load_status() -> dict:
    """Consulta cacheada (2 min) del estado de las 5 nubes. Ambiente commercial."""
    data = cstatus.fetch_all()
    return {
        name: {
            "provider": cs.provider,
            "indicator": cs.indicator,
            "summary": cs.summary,
            "incidents": cs.incidents,
            "affected_regions": cs.affected_regions,
            "status_url": cs.status_url,
            "fetched_at": cs.fetched_at,
            "error": cs.error,
        }
        for name, cs in data.items()
    }


def _fmt_timestamp(ts: str) -> str:
    """Convierte 'YYYY-mm-dd HH:MM:SS' a '02 Oct 2026 · 16:47'."""
    try:
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        meses = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
                 "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        return f"{d.day:02d} {meses[d.month-1]} {d.year} · {d.hour:02d}:{d.minute:02d}"
    except Exception:  # noqa: BLE001
        return ts or "—"


# ===========================================================================
# Resumen global + accion de actualizacion
# ===========================================================================
def render_overview(data: dict) -> None:
    total = len(_PROVIDERS)
    operativos = sum(1 for p in _PROVIDERS if data.get(p, {}).get("indicator") == "ok")
    con_incidente = sum(
        1 for p in _PROVIDERS
        if data.get(p, {}).get("indicator") in ("minor", "major", "critical")
    )

    if operativos == total:
        pill_bg, pill_fg, pill_txt = "#e6f4ea", "#1a7a4a", "🟢 Todo operativo"
    elif con_incidente == 0:
        pill_bg, pill_fg, pill_txt = "#eceff1", "#546e7a", "⚪ Datos parciales"
    elif con_incidente == 1:
        pill_bg, pill_fg, pill_txt = "#fff8e1", "#b7791f", "🟡 Atencion"
    else:
        pill_bg, pill_fg, pill_txt = "#fdecea", "#c0392b", "🔴 Multiples incidentes"

    fetched = next((data[p]["fetched_at"] for p in _PROVIDERS if data.get(p)), "")

    left, right = st.columns([0.68, 0.32])
    with left:
        st.markdown(
            f"""
            <div class='cops-overview'>
                <span class='big' style='color:{pill_fg};'>{operativos}/{total}</span>
                <div style='display:flex; flex-direction:column;'>
                    <span class='lbl'>Estado general de infraestructura</span>
                    <span style='font-size:.8rem;color:#8894a6;'>{operativos} operativos · {con_incidente} con incidencias</span>
                </div>
                <span class='pill' style='background:{pill_bg};color:{pill_fg};margin-left:auto;'>{pill_txt}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
        if st.button("🔄 Actualizar estado", type="primary", use_container_width=True,
                     key="btn_refresh"):
            _load_status.clear()
            st.rerun()
        st.markdown(
            f"<div style='text-align:right;font-size:.76rem;color:#8894a6;margin-top:4px;'>"
            f"Ultima actualizacion: <b style='color:#5b6472'>{_fmt_timestamp(fetched)}</b>"
            f" · auto cada 2 min</div>",
            unsafe_allow_html=True,
        )


# ===========================================================================
# Tarjetas de estado por nube
# ===========================================================================
def render_cards(data: dict) -> None:
    st.markdown("<div class='cops-section'><span class='bar'></span>Estado de salud de las nubes "
                "<span style='font-weight:500;color:#8894a6;font-size:.86rem;'>· fuentes oficiales</span></div>",
                unsafe_allow_html=True)

    cards_html = "<div class='cloud-grid'>"
    for name in _PROVIDERS:
        info = data.get(name, {"indicator": "unknown", "summary": "Sin datos",
                               "affected_regions": [], "status_url": "#"})
        ind = info.get("indicator", "unknown")
        emoji, label, bcolor, bbg = _STATE.get(ind, _STATE["unknown"])
        brand = _CLOUD_BRAND.get(name, _KYNDRYL_BLUE)
        regions = info.get("affected_regions", [])
        region_txt = (
            "Regiones: " + ", ".join(regions[:2]) + (" …" if len(regions) > 2 else "")
            if regions else "Sin regiones afectadas"
        )
        url = info.get("status_url", "#")
        cards_html += f"""
        <div class='ccard' style='--brand:{brand}; --bcolor:{bcolor}; --bbg:{bbg};'>
            <div class='logo'>{_logo_svg(name)}</div>
            <div class='pname'>{name}</div>
            <span class='badge'><span class='dot'>{emoji}</span>{label}</span>
            <div class='desc'>{info.get('summary','')}</div>
            <div class='regions'>{region_txt}</div>
            <a class='official' href='{url}' target='_blank'>🔗 Ver estado oficial</a>
        </div>"""
    cards_html += "</div>"
    st.markdown(cards_html, unsafe_allow_html=True)


# ===========================================================================
# Resumen de incidentes activos
# ===========================================================================
def render_incidents(data: dict) -> None:
    rows = []
    for name in _PROVIDERS:
        for inc in data.get(name, {}).get("incidents", []):
            rows.append({
                "Proveedor": name,
                "Region": inc.get("region", ""),
                "Servicio": inc.get("service", ""),
                "Estado": inc.get("status", ""),
                "Descripcion": (inc.get("title", "") or "")[:140],
                "Actualizado": inc.get("updated", ""),
            })

    n = len(rows)
    dot = "🟢" if n == 0 else ("🟡" if n <= 2 else "🔴")
    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)
    with st.expander(f"{dot}  Incidentes activos — {n}", expanded=bool(n)):
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown(
                "<div style='padding:.5rem .2rem;color:#1a7a4a;font-weight:600;'>"
                "✅ No hay incidentes activos reportados en las fuentes oficiales.</div>",
                unsafe_allow_html=True,
            )

    errs = {p: data[p]["error"] for p in _PROVIDERS if data.get(p, {}).get("error")}
    if errs:
        with st.expander("⚠️ Avisos de consulta", expanded=False):
            for p, e in errs.items():
                st.warning(f"{p}: {e}")


# ===========================================================================
# Dashboard de salud (orquesta overview + cards + incidentes)
# ===========================================================================
def render_health_dashboard() -> None:
    with st.spinner("Consultando sitios oficiales de estado…"):
        data = _load_status()
    render_overview(data)
    render_cards(data)
    render_incidents(data)


# ===========================================================================
# Conexiones Cloud (login)
# ===========================================================================
def _login_result(cloud: str, ok: bool, msg: str) -> None:
    key = f"session_{cloud}"
    if ok:
        st.session_state[key] = {"connected": True, "at": datetime.now().isoformat()}
        st.session_state["_flash"] = ("success", msg)
        # Re-render para que el badge del header refleje 'Conectado' de inmediato.
        st.rerun()
    else:
        st.error(msg)


def _login_header(provider: str, method: str) -> None:
    connected = st.session_state.get(f"session_{provider}", {}).get("connected")
    badge = (
        "<span class='conn-badge conn-on'>● Conectado</span>" if connected
        else "<span class='conn-badge conn-off'>○ Sin conectar</span>"
    )
    st.markdown(
        f"""
        <div class='login-head'>
            <div class='lg'>{_logo_svg(provider)}</div>
            <div style='flex:1'>
                <div class='nm'>{provider}</div>
                <div class='mt'>{method}</div>
            </div>
            {badge}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _aws_login_form() -> None:
    _login_header("AWS", "IAM Identity Center (SSO) · portal por cliente")

    cliente = st.selectbox(
        "🏢 Cliente",
        options=list(_AWS_CLIENTS.keys()),
        key="aws_cliente",
        help="Selecciona el cliente para abrir su portal de AWS IAM Identity Center.",
    )
    url = _AWS_CLIENTS.get(cliente, "")

    if not url:
        st.info(f"El portal de {cliente} aun no esta configurado. "
                "Proximamente se agregara su enlace.")
        return

    st.caption(f"Portal SSO: {aws_auth.normalize_start_url(url)}")

    # Fase A — iniciar device authorization (si no hay una en curso).
    pending = st.session_state.get("aws_device_auth")
    if not pending or st.session_state.get("aws_device_client") != cliente:
        if st.button("Conectar AWS", key="btn_aws", type="primary",
                     use_container_width=True):
            with st.spinner("Preparando aprobacion de acceso en AWS…"):
                da = aws_auth.begin_device_authorization(url)
            if not da.ok:
                _login_result("AWS", False, f"No se pudo iniciar el acceso. {da.error}")
            else:
                st.session_state["aws_device_auth"] = da
                st.session_state["aws_device_client"] = cliente
                st.rerun()
        return

    # Fase B — mostrar enlace de aprobacion y completar tras aprobar.
    da = pending
    approve_url = da.verification_uri_complete or da.verification_uri
    st.markdown(
        f"<a href='{approve_url}' target='_blank' class='aws-portal-btn'>"
        f"🔓 Aprobar acceso en AWS · {cliente}</a>",
        unsafe_allow_html=True,
    )
    st.caption(f"Codigo de verificacion: **{da.user_code}** · "
               "Abre el enlace, aprueba el acceso y vuelve aqui.")

    cols = st.columns([0.6, 0.4])
    if cols[0].button("Ya aprobe · continuar", key="btn_aws_complete",
                      type="primary", use_container_width=True):
        with st.spinner("Verificando aprobacion y cargando cuentas…"):
            conn = aws_auth.complete_login(da, client_name=cliente, max_wait=12)
        if not conn.ok:
            # Mantiene el device_auth para poder reintentar sin reiniciar el flujo.
            st.error(conn.error)
            st.info("Si ya aprobaste en el navegador, espera unos segundos y "
                    "pulsa de nuevo 'Ya aprobe · continuar'.")
        else:
            st.session_state["aws_connection"] = conn
            st.session_state["aws_cliente_sel"] = cliente
            st.session_state["aws_portal_url"] = da.start_url
            st.session_state.pop("aws_device_auth", None)
            st.session_state.pop("aws_device_client", None)
            st.session_state["session_AWS"] = {
                "connected": True,
                "at": datetime.now().isoformat(),
                "mode": f"SSO · {cliente}",
            }
            # Persistir sesion para sobrevivir a un F5 (mientras el token siga vigente).
            session_store.save_aws(
                access_token=conn.access_token,
                start_url=conn.start_url,
                sso_region=conn.sso_region,
                client_name=cliente,
                expires_at=conn.expires_at,
            )
            st.success(f"AWS ({cliente}) conectado. "
                       "Las cuentas se listaran en el panel central.")
            # Re-render para que el badge del header pase a 'Conectado' en este mismo flujo.
            st.rerun()
    if cols[1].button("Cancelar", key="btn_aws_cancel", type="secondary",
                      use_container_width=True):
        st.session_state.pop("aws_device_auth", None)
        st.session_state.pop("aws_device_client", None)
        st.rerun()


def _azure_login_form() -> None:
    _login_header("Azure", "Interactivo (navegador) · az login · Service Principal")
    mode = st.radio(
        "Metodo de autenticacion",
        ["Interactivo (navegador)", "az login (default)", "Service Principal"],
        key="azure_mode", horizontal=True,
    )
    if mode == "Service Principal":
        c1, c2 = st.columns(2)
        c1.text_input("🏢 Tenant ID", key="az_tenant")
        c2.text_input("🆔 Client ID", key="az_client")
        st.text_input("🔑 Client Secret", key="az_secret", type="password")
    elif mode == "az login (default)":
        st.caption("Usa la sesion activa de `az login` en tu equipo. Las suscripciones "
                   "y tenants se seleccionaran luego en el panel central.")
    else:  # Interactivo (navegador)
        st.caption("Se abre el navegador para autenticar. Tras iniciar sesion quedaras "
                   "logueado; la seleccion de tenants se hara en el panel central.")

    if mode == "Interactivo (navegador)":
        _azure_interactive_connect()
        return

    if st.button("Conectar Azure", key="btn_azure", type="primary", use_container_width=True):
        st.session_state["azure_auth_mode"] = mode
        if mode == "az login (default)":
            # TODO (siguiente modulo): AzureCliCredential real.
            _login_result("Azure", True, "Sesion Azure (az login) preparada. "
                                         "Selecciona los tenants en el panel central.")
        else:  # Service Principal
            if not st.session_state.get("az_tenant", "").strip():
                _login_result("Azure", False,
                              "Indica Tenant ID / Client ID / Secret para el Service Principal.")
            else:
                # TODO (siguiente modulo): ClientSecretCredential real.
                _login_result("Azure", True, "Sesion Azure (Service Principal) preparada. "
                                             "Selecciona los tenants en el panel central.")


def _azure_interactive_connect() -> None:
    """Login de Azure por DEVICE CODE (dos pasos, estable con Streamlit).

    Fase A: 'Conectar Azure' inicia el device code y muestra el codigo.
    Fase B: tras ingresar el codigo en el navegador, 'Continuar' lee los tenants.
    """
    da = st.session_state.get("azure_device_auth")

    # Fase A — iniciar y mostrar el codigo.
    if not da:
        if st.button("Conectar Azure", key="btn_azure", type="primary",
                     use_container_width=True):
            with st.spinner("Generando codigo de dispositivo…"):
                da = azure_auth.begin_device_login()
            if not da.ok:
                _login_result("Azure", False, f"No se pudo iniciar el login. {da.error}")
            else:
                st.session_state["azure_device_auth"] = da
                st.rerun()
        return

    # Fase B — mostrar codigo + completar.
    uri = da.verification_uri or "https://microsoft.com/devicelogin"
    st.markdown(
        f"<a href='{uri}' target='_blank' class='aws-portal-btn' "
        f"style='background:#0078D4;color:#fff !important;border-color:#0067b8;'>"
        f"🔑 Abrir login de Azure</a>",
        unsafe_allow_html=True,
    )
    st.info(f"Ingresa este codigo en la pagina:  **{da.user_code}**")

    cols = st.columns([0.6, 0.4])
    if cols[0].button("Ya ingrese el codigo · continuar", key="btn_azure_done",
                      type="primary", use_container_width=True):
        with st.spinner("Verificando y leyendo tenants…"):
            conn = azure_auth.finish_login(da, wait=4)
        if conn.error == "pending":
            st.warning("Aun no se detecta la aprobacion. Ingresa el codigo en el "
                       "navegador y vuelve a pulsar 'Continuar'.")
        elif not conn.ok:
            st.error(f"No se pudo conectar a Azure. {conn.error}")
            st.session_state.pop("azure_device_auth", None)
        else:
            st.session_state["azure_connection"] = conn
            st.session_state["azure_auth_mode"] = "Interactivo (navegador)"
            st.session_state.pop("azure_device_auth", None)
            st.session_state["session_Azure"] = {
                "connected": True,
                "at": datetime.now().isoformat(),
                "mode": "Interactivo (navegador)",
            }
            # Persistir sesion para sobrevivir a un F5 (sin reautenticar).
            _persist_azure(conn)
            acct = conn.account_hint or "cuenta Azure"
            st.success(f"Azure conectado como {acct}. "
                       f"{len(conn.tenants)} tenant(s) disponible(s). "
                       "Elige el tenant en el panel central.")
            # Re-render para que el badge del header pase a 'Conectado' en este mismo flujo.
            st.rerun()
    if cols[1].button("Cancelar", key="btn_azure_cancel", type="secondary",
                      use_container_width=True):
        st.session_state.pop("azure_device_auth", None)
        st.rerun()


def _gcp_login_form() -> None:
    _login_header("GCP", "Service Account (JSON) o Application Default Credentials")
    method = st.radio("Metodo de autenticacion", ["ADC (gcloud)", "Service Account (JSON)"],
                      key="gcp_mode", horizontal=True)
    if method == "Service Account (JSON)":
        st.file_uploader("🔐 Clave de cuenta de servicio (.json)", type=["json"], key="gcp_sa")
    st.text_input("📦 Project ID", key="gcp_project", placeholder="mi-proyecto-123")
    if st.button("Conectar GCP", key="btn_gcp", type="primary", use_container_width=True):
        # TODO (siguiente modulo): google-auth / google-cloud real.
        _login_result("GCP", True, "Conexion GCP preparada.")


def _oci_login_form() -> None:
    _login_header("OCI", "Config file (~/.oci/config) o entrada manual")
    method = st.radio("Metodo de autenticacion", ["Config file (~/.oci/config)", "Entrada manual"],
                      key="oci_mode", horizontal=True)
    if method == "Entrada manual":
        c1, c2 = st.columns(2)
        c1.text_input("🏷️ Tenancy OCID", key="oci_tenancy")
        c2.text_input("👤 User OCID", key="oci_user")
        c3, c4 = st.columns(2)
        c3.text_input("🌎 Region", key="oci_region", value="us-ashburn-1")
        c4.text_input("🔑 Fingerprint", key="oci_fp")
    else:
        st.text_input("📄 Profile", key="oci_profile", value="DEFAULT")
    if st.button("Conectar OCI", key="btn_oci", type="primary", use_container_width=True):
        # TODO (siguiente modulo): oci SDK real.
        _login_result("OCI", True, "Conexion OCI preparada.")


def _ibm_login_form() -> None:
    _login_header("IBM Cloud", "API Key (IAM)")
    c1, c2 = st.columns(2)
    c1.text_input("🔑 IBM Cloud API Key", key="ibm_apikey", type="password")
    c2.text_input("🏢 Account ID (opcional)", key="ibm_account")
    if st.button("Conectar IBM Cloud", key="btn_ibm", type="primary", use_container_width=True):
        if not st.session_state.get("ibm_apikey"):
            _login_result("IBM Cloud", False, "Indica tu API Key de IBM Cloud.")
        else:
            # TODO (siguiente modulo): ibm-platform-services real.
            _login_result("IBM Cloud", True, "Conexion IBM Cloud preparada.")


def render_login() -> None:
    # Mensaje diferido tras un st.rerun() (p. ej. desde _login_result).
    flash = st.session_state.pop("_flash", None)
    if flash:
        level, msg = flash
        getattr(st, level, st.info)(msg)

    st.markdown("<div style='height:.8rem'></div>", unsafe_allow_html=True)
    st.markdown("<div class='cops-section'><span class='bar'></span>Conexiones Cloud</div>",
                unsafe_allow_html=True)
    st.caption("Conecta una o varias nubes. En este modulo se validan los datos de acceso; "
               "el descubrimiento de recursos llegara en los siguientes modulos.")

    tabs = st.tabs([f"  {p}  " for p in _PROVIDERS])
    with tabs[0]:
        _aws_login_form()
    with tabs[1]:
        _azure_login_form()
    with tabs[2]:
        _gcp_login_form()
    with tabs[3]:
        _oci_login_form()
    with tabs[4]:
        _ibm_login_form()

    connected = _connected_clouds()
    if connected:
        st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
        c1, c2 = st.columns([0.62, 0.38])
        with c1:
            st.success("Nubes conectadas: " + ", ".join(connected))
        with c2:
            if st.button("Entrar al panel  ➜", type="primary", use_container_width=True,
                         key="btn_enter_panel"):
                st.session_state["view"] = "panel"
                st.rerun()


# ===========================================================================
# Estado de sesion / navegacion
# ===========================================================================
def _connected_clouds() -> list:
    return [p for p in _PROVIDERS
            if st.session_state.get(f"session_{p}", {}).get("connected")]


def _persist_azure(conn) -> None:
    """Serializa el AuthenticationRecord de Azure y lo guarda para reanudar."""
    try:
        record = getattr(conn, "record", None)
        if record is None:
            return
        tenants = [{"id": t.id, "display_name": t.display_name,
                    "default_domain": getattr(t, "default_domain", "")}
                   for t in getattr(conn, "tenants", [])]
        session_store.save_azure(
            record_serialized=record.serialize(),
            account_hint=getattr(conn, "account_hint", ""),
            tenants=tenants,
        )
    except Exception:  # noqa: BLE001
        pass


def _restore_session() -> None:
    """Restaura la sesion desde el store tras un F5, sin reautenticar.

    Se ejecuta una sola vez por carga del navegador. Reconstruye las conexiones
    que sigan vigentes y, si al menos una lo esta, deja la vista en 'panel'.
    """
    if st.session_state.get("_session_restored"):
        return
    st.session_state["_session_restored"] = True

    restored_any = False

    # --- Azure: reconstruir credencial desde el record persistido ---
    if not st.session_state.get("session_Azure", {}).get("connected"):
        az = session_store.load_azure()
        if az:
            conn = azure_auth.restore_connection(az["record"])
            if conn.ok:
                # Reusar los nombres de tenant ya guardados (evita re-consultar
                # Graph en cada F5 y conserva el nombre si Graph no respondiera).
                saved = {t.get("id"): t for t in az.get("tenants", [])}
                for t in conn.tenants:
                    s = saved.get(t.id)
                    if s:
                        if not t.display_name and s.get("display_name"):
                            t.display_name = s["display_name"]
                        if not t.default_domain and s.get("default_domain"):
                            t.default_domain = s["default_domain"]
                st.session_state["azure_connection"] = conn
                st.session_state["azure_auth_mode"] = "Interactivo (navegador)"
                st.session_state["session_Azure"] = {
                    "connected": True,
                    "at": datetime.now().isoformat(),
                    "mode": "Interactivo (navegador)",
                }
                # Re-persistir por si Graph resolvio nombres nuevos esta vez.
                _persist_azure(conn)
                restored_any = True
            else:
                # Record invalido/expirado: descartarlo para pedir login limpio.
                session_store.drop_azure()

    # --- AWS: reutilizar el token SSO si sigue vigente ---
    if not st.session_state.get("session_AWS", {}).get("connected"):
        aws = session_store.load_aws()
        if aws:
            conn = aws_auth.AWSConnection(
                client_name=aws.get("client_name", ""),
                start_url=aws.get("start_url", ""),
                access_token=aws.get("access_token", ""),
                expires_at=aws.get("expires_at", 0),
                sso_region=aws.get("sso_region", "us-east-1"),
            )
            st.session_state["aws_connection"] = conn
            st.session_state["aws_cliente_sel"] = aws.get("client_name", "")
            st.session_state["aws_portal_url"] = aws.get("start_url", "")
            st.session_state["session_AWS"] = {
                "connected": True,
                "at": datetime.now().isoformat(),
                "mode": f"SSO · {aws.get('client_name', '')}",
            }
            restored_any = True

    if restored_any:
        st.session_state["view"] = "panel"


def _logout() -> None:
    """Cierra sesion: limpia conexiones y vuelve al login."""
    for p in _PROVIDERS:
        st.session_state.pop(f"session_{p}", None)
    # Limpia conexiones reales y selecciones de alcance.
    for k in ("azure_connection", "azure_device_auth", "aws_connection",
              "aws_device_auth", "aws_device_client", "aws_cliente_sel",
              "aws_portal_url"):
        st.session_state.pop(k, None)
    for k in list(st.session_state.keys()):
        if k.startswith("scope_"):
            st.session_state.pop(k, None)
    # Borrar la sesion persistida para que el F5 no la reviva.
    session_store.clear()
    st.session_state["view"] = "login"


# ===========================================================================
# Panel central — Sidebar
# ===========================================================================
# Placeholders de alcance por nube (datos de ejemplo mientras definimos el modulo;
# se reemplazaran por descubrimiento real via SDK en el siguiente modulo).
_SCOPE_PLACEHOLDERS = {
    "AWS":       ("Cuentas",       ["111122223333 · prod", "444455556666 · dev"]),
    "Azure":     ("Tenants / Subs", ["Contoso (tenant) · Sub-Prod", "Contoso (tenant) · Sub-Dev"]),
    "GCP":       ("Proyectos",     ["mi-proyecto-prod", "mi-proyecto-dev"]),
    "OCI":       ("Compartments",  ["root", "network", "workloads"]),
    "IBM Cloud": ("Resource Groups", ["Default", "produccion"]),
}


def _sidebar_azure_scope() -> None:
    """Tenants LEIDOS en el login. El login al tenant elegido se hace aqui,
    en el panel central (se cableara en el siguiente paso del modulo)."""
    conn = st.session_state.get("azure_connection")
    tenants = getattr(conn, "tenants", []) if conn else []
    with st.expander(f"Azure · Tenants ({len(tenants)})", expanded=True):
        acct = getattr(conn, "account_hint", "") if conn else ""
        if acct:
            st.caption(f"Usuario: {acct}")
        if not tenants:
            st.caption("No se leyeron tenants.")
            return

        tenant_labels = {t.label: t for t in tenants}
        sel_label = st.selectbox(
            "Tenant",
            options=list(tenant_labels.keys()),
            key="scope_Azure_tenant",
            help="Selecciona el tenant con el que trabajaras.",
        )
        sel_tenant = tenant_labels.get(sel_label)
        if sel_tenant and sel_tenant.id and sel_tenant.id != sel_label:
            st.caption(f"ID: {sel_tenant.id}")
        st.caption("El inicio de sesion en el tenant seleccionado se realizara "
                   "en este panel (proximo paso).")


@st.cache_data(ttl=600, show_spinner=False)
def _aws_accounts(token: str, region: str) -> list:
    """Lista cuentas AWS (cacheada por token). Devuelve [(label, account_id)]."""
    accts = aws_auth.list_accounts(token, region)
    return [(a.label, a.account_id) for a in accts]


@st.cache_data(ttl=600, show_spinner=False)
def _aws_roles(token: str, region: str, account_id: str) -> list:
    """Lista roles de UNA cuenta (cacheada). Devuelve [role_name]."""
    roles = aws_auth.list_account_roles(token, account_id, region)
    return [r.role_name for r in roles]


def _aws_token_expired(conn) -> bool:
    """True si el token SSO ya expiro (o esta por expirar en <60s)."""
    exp = float(getattr(conn, "expires_at", 0) or 0)
    return exp > 0 and exp <= datetime.now().timestamp() + 60


def _aws_reauth_prompt(cliente: str, reason: str) -> None:
    """Reabre el flujo de login SSO del cliente actual (MFA en el navegador).

    Se usa cuando el token expiro o la cuenta elegida exige una sesion nueva.
    Reutiliza el mismo device authorization del login para el portal del cliente.
    """
    url = _AWS_CLIENTS.get(cliente, "")
    st.warning(reason)
    if not url:
        st.caption(f"El portal de {cliente} aun no esta configurado.")
        return

    rk_auth = f"aws_reauth_{cliente}"
    da = st.session_state.get(rk_auth)

    # Fase A — iniciar reautenticacion.
    if not da:
        if st.button(f"Reautenticar {cliente} (MFA)", key=f"btn_reauth_{cliente}",
                     type="primary", use_container_width=True):
            with st.spinner("Preparando aprobacion de acceso en AWS…"):
                da = aws_auth.begin_device_authorization(url)
            if not da.ok:
                st.error(f"No se pudo iniciar la reautenticacion. {da.error}")
            else:
                st.session_state[rk_auth] = da
                st.rerun()
        return

    # Fase B — mostrar enlace de aprobacion y completar.
    approve_url = da.verification_uri_complete or da.verification_uri
    st.markdown(
        f"<a href='{approve_url}' target='_blank' class='aws-portal-btn'>"
        f"🔓 Aprobar acceso · {cliente}</a>",
        unsafe_allow_html=True,
    )
    st.caption(f"Codigo: **{da.user_code}** · Abre el enlace, aprueba (MFA) y vuelve.")
    if st.button("Ya aprobe · continuar", key=f"btn_reauth_done_{cliente}",
                 type="primary", use_container_width=True):
        with st.spinner("Verificando aprobacion…"):
            conn = aws_auth.complete_login(da, client_name=cliente, max_wait=12)
        if not conn.ok:
            st.error(conn.error)
        else:
            st.session_state["aws_connection"] = conn
            st.session_state.pop(rk_auth, None)
            session_store.save_aws(
                access_token=conn.access_token, start_url=conn.start_url,
                sso_region=conn.sso_region, client_name=cliente,
                expires_at=conn.expires_at)
            # Invalida la cache de cuentas para que se recarguen con el token nuevo.
            _aws_accounts.clear()
            _aws_roles.clear()
            st.rerun()


def _sidebar_aws_scope() -> None:
    """Cuentas/roles REALES del cliente AWS, listados BAJO DEMANDA en el panel
    (igual que Azure con sus tenants). El token se obtuvo en el login.

    Si el token SSO expiro, se ofrece reautenticar (MFA) con el portal del
    cliente activo sin salir del panel."""
    cliente = st.session_state.get("aws_cliente_sel", "")
    conn = st.session_state.get("aws_connection")
    token = getattr(conn, "access_token", "") if conn else ""
    region = getattr(conn, "sso_region", "us-east-1") if conn else "us-east-1"

    with st.expander(f"AWS · {cliente}", expanded=True):
        if not token:
            st.caption("Sin token SSO.")
            return

        # Token expirado -> pedir MFA del cliente actual.
        if _aws_token_expired(conn):
            _aws_reauth_prompt(
                cliente,
                "La sesion de AWS expiro. Reautentica con MFA para continuar.")
            return

        with st.spinner("Cargando cuentas…"):
            accounts = _aws_accounts(token, region)
        if not accounts:
            # Sin cuentas suele significar token invalido/expirado -> reautenticar.
            _aws_reauth_prompt(
                cliente,
                "No se pudieron listar cuentas (sesion expirada o sin acceso). "
                "Reautentica con MFA.")
            return

        labels = [lbl for lbl, _ in accounts]
        sel = st.selectbox(f"Cuenta ({len(accounts)})", options=labels,
                           key="scope_AWS_account")
        acc_id = dict(accounts).get(sel, "")

        with st.spinner("Cargando roles…"):
            roles = _aws_roles(token, region, acc_id)
        if roles:
            st.selectbox("Rol", options=roles, key="scope_AWS_role")
        else:
            st.caption("Esta cuenta no expone roles para tu usuario.")


def render_sidebar() -> None:
    with st.sidebar:
        # 1) Logo principal.
        if _LOGO_PATH.exists():
            st.image(str(_LOGO_PATH))
        st.markdown(
            "<div style='font-weight:800;color:#0050A0;font-size:1rem;margin:.2rem 0 .1rem;'>"
            "PLAN-ADM</div>"
            "<div style='color:#8894a6;font-size:.74rem;margin-bottom:.6rem;'>"
            "Cloud Operations</div>",
            unsafe_allow_html=True,
        )
        st.divider()

        connected = _connected_clouds()

        # 2) Nubes activas.
        st.markdown("<div class='sb-title'>Nubes activas</div>", unsafe_allow_html=True)
        if connected:
            for p in connected:
                st.markdown(
                    f"<div class='sb-cloud'><span class='sb-dot'></span>{p}</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("Sin nubes conectadas.")

        st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

        # 3) Tenants / cuentas / etc. (selector de alcance por nube conectada).
        st.markdown("<div class='sb-title'>Tenants · cuentas · alcance</div>",
                    unsafe_allow_html=True)
        if connected:
            for p in connected:
                if p == "Azure" and st.session_state.get("azure_connection"):
                    _sidebar_azure_scope()
                elif p == "AWS" and st.session_state.get("aws_cliente_sel"):
                    _sidebar_aws_scope()
                else:
                    label, options = _SCOPE_PLACEHOLDERS.get(p, ("Alcance", []))
                    with st.expander(f"{p} · {label}", expanded=False):
                        st.multiselect(
                            f"Selecciona {label.lower()}",
                            options=options,
                            default=options[:1] if options else [],
                            key=f"scope_{p}",
                            label_visibility="collapsed",
                        )
                        st.caption("Datos de ejemplo · se cargaran via SDK proximamente.")
        else:
            st.caption("Conecta una nube para ver su alcance.")

        st.divider()

        # 4) Cerrar sesion.
        if st.button("🚪 Cerrar sesion", use_container_width=True, key="btn_logout"):
            _logout()
            st.rerun()


# ===========================================================================
# Panel central — Areas de gestion
# ===========================================================================
# Areas del panel central. Cada entrada: (clave, etiqueta, icono, descripcion).
# Por ahora todas son CASCARA: una pestaña por nube, sin logica de negocio.
_PANEL_AREAS = [
    ("health",    "1.1  Health check",  "🩺",
     "Estado de salud por nube."),
    ("costos",    "1.2  Costos",        "💲",
     "Costos y optimizacion por nube."),
    ("seguridad", "1.3  Seguridad",     "🛡️",
     "Postura de seguridad y cumplimiento por nube."),
    ("inventario", "1.4  Inventario",   "🗂️",
     "Inventario de recursos por nube."),
    ("alertas",   "1.5  Alertamiento",  "🔔",
     "Alertas y notificaciones por nube."),
    ("deprecados", "1.6  Recursos deprecados", "⏳",
     "Servicios en proceso de retiro segun anuncios de Microsoft."),
]
_PANEL_LABELS = [a[1] for a in _PANEL_AREAS]
_PANEL_BY_LABEL = {a[1]: a for a in _PANEL_AREAS}


def _not_connected_placeholder(provider: str) -> None:
    """Mensaje para una nube cuya pestaña se muestra pero sin sesion activa."""
    brand = _CLOUD_BRAND.get(provider, _KYNDRYL_BLUE)
    st.markdown(
        f"""
        <div style='border:1.5px dashed #cfd8e3; border-radius:14px;
                    padding:2.4rem 2rem; text-align:center; background:#fbfcfe;
                    color:#8894a6; margin-top:.6rem;'>
            <div style='font-size:1.6rem;'>🔒</div>
            <div style='font-size:1rem;font-weight:700;color:{brand};margin-top:.3rem;'>
                {provider}
            </div>
            <div style='font-size:.9rem;margin-top:.3rem;'>
                Inicia sesion primero para ver el estado de {provider}.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _connected_placeholder(provider: str, area_label: str, icon: str) -> None:
    """Cascara de un area para una nube conectada (sin logica aun)."""
    brand = _CLOUD_BRAND.get(provider, _KYNDRYL_BLUE)
    # Nombre del area sin el prefijo numerico, para el titulo de la tarjeta.
    area_name = area_label.split("  ", 1)[-1] if "  " in area_label else area_label
    st.markdown(
        f"""
        <div style='border:1.5px dashed {brand}55; border-radius:14px;
                    padding:2.4rem 2rem; text-align:center; background:#fbfcfe;
                    color:#8894a6; margin-top:.6rem;'>
            <div style='font-size:1.6rem;'>{icon}</div>
            <div style='font-size:1rem;font-weight:700;color:{brand};margin-top:.3rem;'>
                {area_name} · {provider}
            </div>
            <div style='font-size:.9rem;margin-top:.3rem;'>
                Sesion activa. El contenido de {area_name.lower()} se construira
                en el proximo paso.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ===========================================================================
# Azure Alert Center (1.5 Alertamiento) — datos reales de Azure Monitor
# ===========================================================================
_SEV_UI = {
    "Sev0": ("Critical",      "🔴", "#c0392b", "#fdecea"),
    "Sev1": ("Error / High",  "🟠", "#c05621", "#fff1e6"),
    "Sev2": ("Warning",       "🟠", "#b7791f", "#fff8e1"),
    "Sev3": ("Informational", "🔵", "#2b6cb0", "#e6f0fb"),
    "Sev4": ("Verbose",       "⚪", "#546e7a", "#eceff1"),
}
# Periodos de la UI -> valor de timeRange para la API.
_ALERT_PERIODS = {
    "Última hora": "1h",
    "Últimas 6 horas": "1d",
    "Últimas 24 horas": "1d",
    "Últimos 7 días": "7d",
    "Últimos 30 días": "30d",
}
# Clasificacion de entorno por heuristica de nombre de suscripcion.
_ENV_KEYWORDS = {
    "Producción":   ("prod", "produccion", "prd", "production"),
    "Desarrollo":   ("dev", "develop", "desarrollo"),
    "QA / Testing": ("qa", "test", "testing", "stage", "staging", "uat"),
}


def _azure_credential():
    conn = st.session_state.get("azure_connection")
    return getattr(conn, "credential", None) if conn else None


def _azure_selected_tenant_id():
    conn = st.session_state.get("azure_connection")
    tenants = getattr(conn, "tenants", []) if conn else []
    sel_label = st.session_state.get("scope_Azure_tenant")
    for t in tenants:
        if t.label == sel_label:
            return t.id
    return tenants[0].id if tenants else None


@st.cache_data(ttl=300, show_spinner=False)
def _cached_subscriptions(_cred, tenant_id: str) -> list:
    """Suscripciones del tenant (cacheadas 5 min). _cred no se hashea."""
    return azure_auth.list_subscriptions(_cred, tenant_id)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_alerts(_cred, subs_key: str, subs: list, time_range: str,
                   rg: str):
    """Alertas reales (cacheadas 2 min). _cred y subs no se hashean por valor;
    subs_key identifica el conjunto para el cache."""
    df = azure_alerts.list_alerts(_cred, subs, time_range, rg or None)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _cached_alert_rules(_cred, subs_key: str, subs: list):
    """Reglas de alerta CONFIGURADAS (cacheadas 5 min). Todas las creadas,
    disparadas o no."""
    return azure_alerts.list_alert_rules(_cred, subs)


def _env_of(sub_name: str) -> str:
    low = (sub_name or "").lower()
    for env, kws in _ENV_KEYWORDS.items():
        if any(k in low for k in kws):
            return env
    return "Otros"


def _kpi_card(col, title: str, value, sub: str = "", accent: str = "#0050A0",
              strong: bool = False) -> None:
    bg = "#fff" if not strong else "#fdecea"
    border = accent
    col.markdown(
        f"""
        <div style='background:{bg};border-radius:12px;padding:.8rem 1rem;
                    border-left:5px solid {border};box-shadow:0 1px 3px #0000000f;
                    min-height:92px;'>
            <div style='font-size:.72rem;color:#6b7280;font-weight:700;
                        text-transform:uppercase;letter-spacing:.03em'>{title}</div>
            <div style='font-size:1.8rem;font-weight:800;color:{accent};
                        line-height:1.1;margin-top:.1rem'>{value}</div>
            <div style='font-size:.74rem;color:#8894a6;margin-top:.15rem'>{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _azure_tenant_login_flow(tenant_id) -> None:
    """Inicia sesion CONTRA UN TENANT concreto (dos fases, device code).

    Necesario cuando el tenant exige MFA / acceso condicional propio y la
    credencial actual no basta para descubrir sus suscripciones. Al completarse,
    reemplaza la credencial de la sesion por la nueva (que ya paso el challenge)
    y limpia la cache de suscripciones para re-descubrir."""
    if not tenant_id:
        st.caption("Selecciona un tenant en el panel izquierdo.")
        return

    rk = f"az_tenant_auth_{tenant_id}"
    da = st.session_state.get(rk)

    # Fase A — iniciar el device code para el tenant.
    if not da:
        if st.button("Iniciar sesión en este tenant (MFA)",
                     key=f"btn_tlogin_{tenant_id}", type="primary"):
            with st.spinner("Generando código de dispositivo…"):
                da = azure_auth.begin_device_login(tenant_id=tenant_id)
            if not da.ok:
                st.error(f"No se pudo iniciar el login. {da.error}")
            else:
                st.session_state[rk] = da
                st.rerun()
        return

    # Fase B — mostrar codigo + completar.
    uri = da.verification_uri or "https://microsoft.com/devicelogin"
    st.markdown(
        f"<a href='{uri}' target='_blank' class='aws-portal-btn' "
        f"style='background:#0078D4;color:#fff !important;border-color:#0067b8;'>"
        f"🔑 Abrir login de Azure</a>", unsafe_allow_html=True)
    st.info(f"Ingresa este código en la página:  **{da.user_code}**")

    cols = st.columns([0.6, 0.4])
    if cols[0].button("Ya ingresé el código · continuar",
                      key=f"btn_tlogin_done_{tenant_id}", type="primary",
                      use_container_width=True):
        with st.spinner("Verificando y leyendo suscripciones…"):
            conn = azure_auth.finish_login(da, wait=4)
        if conn.error == "pending":
            st.warning("Aún no se detecta la aprobación. Ingresa el código en "
                       "el navegador y vuelve a pulsar 'Continuar'.")
        elif not conn.ok:
            st.error(f"No se pudo iniciar sesión en el tenant. {conn.error}")
            st.session_state.pop(rk, None)
        else:
            # Reemplaza la credencial de sesion por la que paso el MFA del tenant.
            st.session_state["azure_connection"] = conn
            st.session_state.pop(rk, None)
            _persist_azure(conn)
            _cached_subscriptions.clear()
            _cached_alerts.clear()
            st.success("Sesión iniciada en el tenant. Descubriendo suscripciones…")
            st.rerun()
    if cols[1].button("Cancelar", key=f"btn_tlogin_cancel_{tenant_id}",
                      use_container_width=True):
        st.session_state.pop(rk, None)
        st.rerun()


def render_azure_alert_center() -> None:
    """Azure Alert Center: dashboard operacional de Azure Monitor Alerts."""
    cred = _azure_credential()
    if cred is None:
        st.warning("Sesion de Azure no disponible. Reconecta en el login.")
        return

    tenant_id = _azure_selected_tenant_id()

    # ── Encabezado ──────────────────────────────────────────────────────────
    st.markdown(
        "<div style='display:flex;align-items:baseline;gap:.6rem;margin:.2rem 0 .1rem'>"
        "<span style='font-size:1.25rem;font-weight:800;color:#0050A0'>Azure Alert Center</span>"
        "<span style='font-size:.85rem;color:#8894a6'>Operación y observabilidad</span>"
        "</div>", unsafe_allow_html=True)

    # Suscripciones del tenant.
    subs = _cached_subscriptions(cred, tenant_id or "")
    if not subs:
        st.info("No se encontraron suscripciones accesibles en el tenant "
                "seleccionado. Este tenant puede exigir un inicio de sesion "
                "adicional (MFA / acceso condicional).")
        _azure_tenant_login_flow(tenant_id)
        return

    # La credencial efectiva puede haber sido reemplazada por el login al tenant.
    cred = _azure_credential()

    # Filtros de cabecera. El selector principal es la SUSCRIPCION ('Todas' o
    # una concreta); el entorno es un filtro secundario por heuristica de nombre.
    f1, f2, f3, f4 = st.columns([2.0, 1.2, 1.2, 0.8])
    sub_names = [s["displayName"] or s["subscriptionId"] for s in subs]
    sub_opts = [f"Todas las suscripciones ({len(subs)})"] + sub_names
    sub_choice = f1.selectbox("Suscripción", sub_opts, key="ac_sub_one")
    env = f2.selectbox("Entorno", ["Todos los entornos", "Producción",
                                   "Desarrollo", "QA / Testing"],
                       key="ac_env")
    period = f3.selectbox("Periodo", list(_ALERT_PERIODS.keys()),
                          index=2, key="ac_period")
    with f4:
        st.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        do_refresh = st.button("Actualizar", key="ac_refresh",
                               use_container_width=True)

    # Resolver seleccion: una suscripcion concreta o todas, luego filtro entorno.
    single = None if sub_choice.startswith("Todas las suscripciones") else sub_choice
    chosen = []
    for s in subs:
        nm = s["displayName"] or s["subscriptionId"]
        if single is not None and nm != single:
            continue
        if env != "Todos los entornos" and _env_of(nm) != env:
            continue
        chosen.append(s)
    if not chosen:
        st.warning("Ninguna suscripción coincide con el filtro seleccionado.")
        return

    time_range = _ALERT_PERIODS[period]
    subs_key = "|".join(sorted(s["subscriptionId"] for s in chosen)) + f"@{time_range}"

    subs_only_key = "|".join(sorted(s["subscriptionId"] for s in chosen))

    if do_refresh:
        _cached_alerts.clear()
        _cached_alert_rules.clear()

    with st.spinner("Consultando Azure Monitor Alerts…"):
        df = _cached_alerts(cred, subs_key, chosen, time_range, "")
        rules = _cached_alert_rules(cred, subs_only_key, chosen)

    # Tabla principal = TODAS las reglas creadas + instancias disparadas.
    df_all = azure_alerts.merge_rules_and_instances(rules, df)

    # RG opcional (se puebla con los RG presentes en todas las reglas/alertas).
    rgs = azure_alerts.resource_groups(df_all)
    if rgs:
        rg_sel = st.selectbox("Resource Group", ["Todos"] + rgs, key="ac_rg")
        if rg_sel != "Todos":
            if not df.empty:
                df = df[df["resourceGroup"] == rg_sel]
            if not df_all.empty:
                df_all = df_all[df_all["resourceGroup"] == rg_sel]

    fetched = df.attrs.get("fetched_at", "") or rules.attrs.get("fetched_at", "")
    errors = (df.attrs.get("errors") or []) + (rules.attrs.get("errors") or [])
    scanned = df.attrs.get("subscriptions_scanned", len(chosen))
    n_rules = 0 if df_all is None else len(df_all)
    st.caption(f"Suscripciones consultadas: {scanned} · Reglas/alertas: "
               f"{n_rules} · Última consulta: {fetched}")
    if errors:
        with st.expander(f"Advertencia: {len(errors)} suscripcion(es) sin datos / sin permiso"):
            for e in errors:
                st.text(f"- {e}")

    if (df is None or df.empty) and (df_all is None or df_all.empty):
        st.success("No hay alertas ni reglas en el alcance seleccionado.")
        return

    # ── Tarjetas KPI ─────────────────────────────────────────────────────────
    k = azure_alerts.kpis(df)
    c1, c2, c3, c4, c5 = st.columns(5)
    _kpi_card(c1, "Alertas activas", k["active"], "en curso (Fired)", "#0050A0")
    _kpi_card(c2, "Críticas", k["critical"], "Requieren atención",
              "#c0392b", strong=k["critical"] > 0)
    _kpi_card(c3, "Advertencias", k["warning"], "severidad Warning", "#b7791f")
    _kpi_card(c4, "Informativas", k["informational"], "severidad Info", "#2b6cb0")
    _kpi_card(c5, "Alertas disparadas", k["fired"],
              f"instancias en {period.lower()}", "#5b21b6")

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)

    # ── Distribucion por severidad + Recursos con mas alertas ────────────────
    col_sev, col_res = st.columns([1.2, 1])
    with col_sev:
        st.markdown("**Alertas por severidad**")
        sev_df = azure_alerts.by_severity(df)
        if sev_df.empty:
            st.caption("Sin datos.")
        else:
            for _, r in sev_df.iterrows():
                lbl, _emo, color, _bg = _SEV_UI.get(r["severity"],
                                                    (r["severityLabel"], "⚪",
                                                     "#546e7a", "#eceff1"))
                pct = r["pct"]
                st.markdown(
                    f"""
                    <div style='margin-bottom:.4rem'>
                      <div style='display:flex;justify-content:space-between;
                                  font-size:.8rem;margin-bottom:2px'>
                        <span style='font-weight:700;color:{color}'>{lbl}</span>
                        <span style='color:#6b7280'>{int(r['count'])} · {pct}%</span>
                      </div>
                      <div style='background:#eef1f6;border-radius:6px;height:12px'>
                        <div style='width:{pct}%;background:{color};height:12px;
                                    border-radius:6px'></div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

    with col_res:
        st.markdown("**Recursos con más alertas**")
        res_df = azure_alerts.top_resources(df, top=10)
        if res_df.empty:
            st.caption("Sin datos de recursos.")
        else:
            for _, r in res_df.iterrows():
                crit_badge = (f"<span style='color:#c0392b;font-weight:700'>"
                              f"{int(r['critical'])} crít.</span>"
                              if r["critical"] else "")
                st.markdown(
                    f"""
                    <div style='border-bottom:1px solid #eef1f6;padding:.3rem 0'>
                      <div style='font-weight:700;color:#1A1A2E;font-size:.86rem'>
                        {r['resource']}</div>
                      <div style='font-size:.74rem;color:#8894a6'>
                        {r['resourceType'] or '—'} · {r['resourceGroup'] or '—'}</div>
                      <div style='font-size:.8rem;color:#0050A0;font-weight:700'>
                        {int(r['alerts'])} alertas &nbsp; {crit_badge}</div>
                    </div>
                    """, unsafe_allow_html=True)

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)

    # ── Alertas mas disparadas ───────────────────────────────────────────────
    st.markdown("**Alertas más disparadas**")
    st.caption("Ordenadas por frecuencia · identifica ruido y problemas recurrentes")
    fired_df = azure_alerts.most_fired(df, top=10)
    if fired_df.empty:
        st.caption("Sin datos.")
    else:
        freq_color = {"Alta": "#c0392b", "Media": "#b7791f", "Baja": "#2b6cb0"}
        for _, r in fired_df.iterrows():
            lbl, _emo, color, _bg = _SEV_UI.get(r["severity"],
                                                (r["severityLabel"], "⚪",
                                                 "#546e7a", "#eceff1"))
            estado = "Activa" if str(r["monitorCondition"]).lower() == "fired" else "Resuelta"
            fc = freq_color.get(r["frequency"], "#6b7280")
            with st.container(border=True):
                a, b, c = st.columns([3, 1.4, 1.2])
                a.markdown(
                    f"<div style='font-weight:700'>{r['name']}</div>"
                    f"<div style='font-size:.78rem;color:#8894a6'>"
                    f"{r['resource'] or '—'} · {r['resourceType'] or '—'} · "
                    f"{r['subscriptionName']}</div>",
                    unsafe_allow_html=True)
                b.markdown(
                    f"<span style='color:{color};font-weight:800'>{lbl}</span><br>"
                    f"<span style='font-size:.76rem'>Estado: {estado}</span>",
                    unsafe_allow_html=True)
                c.markdown(
                    f"<div style='font-size:1.3rem;font-weight:800;color:#1A1A2E'>"
                    f"{int(r['firedTimes'])}</div>"
                    f"<div style='font-size:.72rem'>disparos · "
                    f"<span style='color:{fc};font-weight:700'>{r['frequency']}</span></div>"
                    f"<div style='font-size:.72rem;color:#8894a6'>{r['lastFiredAgo']}</div>",
                    unsafe_allow_html=True)

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)

    # ── Alertas ACTIVAS por nombre + detalle ─────────────────────────────────
    _render_active_alerts(df_all)

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)

    # ── Tabla principal: TODAS las reglas de alerta creadas ──────────────────
    st.markdown("**Alertas** · todas las reglas configuradas en Azure Monitor")
    tf1, tf2, tf3, tf4 = st.columns([1, 1, 1, 2])
    sev_filter = tf1.multiselect(
        "Severidad",
        options=[v[0] for v in _SEV_UI.values()],
        key="ac_tbl_sev")
    estado_filter = tf2.selectbox(
        "Estado", ["Todas", "Activa (disparada)", "Sin disparos",
                   "Habilitada", "Deshabilitada"],
        key="ac_tbl_state")
    tipo_opts = ["Todos"] + azure_alerts.resource_types(df_all)
    tipo_filter = tf3.selectbox("Tipo de recurso", tipo_opts, key="ac_tbl_type")
    search = tf4.text_input("Buscar (alerta, recurso, RG, suscripción)",
                            key="ac_tbl_search")

    tbl = df_all.copy()
    if sev_filter:
        label_to_sev = {v[0]: s for s, v in _SEV_UI.items()}
        wanted = {label_to_sev[l] for l in sev_filter if l in label_to_sev}
        tbl = tbl[tbl["severity"].isin(wanted)]
    if estado_filter == "Activa (disparada)":
        tbl = tbl[tbl["monitorCondition"].str.lower() == "fired"]
    elif estado_filter == "Sin disparos":
        tbl = tbl[tbl["firedTimes"].fillna(0) == 0]
    elif estado_filter == "Habilitada":
        tbl = tbl[tbl["enabled"] == True]  # noqa: E712
    elif estado_filter == "Deshabilitada":
        tbl = tbl[tbl["enabled"] == False]  # noqa: E712
    if tipo_filter != "Todos":
        tbl = tbl[tbl["resourceType"] == tipo_filter]
    if search:
        s = search.lower()
        mask = (tbl["name"].str.lower().str.contains(s, na=False)
                | tbl["resource"].str.lower().str.contains(s, na=False)
                | tbl["resourceGroup"].str.lower().str.contains(s, na=False)
                | tbl["subscriptionName"].str.lower().str.contains(s, na=False))
        tbl = tbl[mask]

    # Columna de estado legible.
    def _estado_legible(r):
        if str(r.get("monitorCondition", "")).lower() == "fired":
            return "Activa (disparada)"
        if not r.get("enabled", True):
            return "Deshabilitada"
        return "Habilitada (sin disparos)"
    tbl = tbl.copy()
    tbl["estadoLegible"] = tbl.apply(_estado_legible, axis=1)

    view_cols = {
        "severityLabel": "Severidad", "name": "Regla / alerta",
        "ruleKind": "Tipo de alerta", "estadoLegible": "Estado",
        "resource": "Recurso", "resourceType": "Tipo recurso",
        "resourceGroup": "Resource Group", "subscriptionName": "Suscripción",
        "firedTimes": "Disparos", "lastFiredAgo": "Último disparo",
    }
    show = tbl[list(view_cols.keys())].rename(columns=view_cols)
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption(f"Total reglas / alertas: {len(tbl)}")

    # Exportar.
    import io as _io
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = _io.StringIO()
    tbl.to_csv(buf, index=False)
    st.download_button("Descargar CSV", buf.getvalue().encode("utf-8-sig"),
                       f"azure_alertas_{ts}.csv", "text/csv", key="ac_csv")

    # ── Detalle de cualquier regla/alerta de la tabla ────────────────────────
    st.markdown("**Detalle de regla / alerta** · cualquiera de la tabla de arriba")
    if tbl.empty:
        st.caption("No hay alertas que mostrar con los filtros actuales.")
        return
    opt_labels = [f"{row['name']} · {row['resource'] or '—'} "
                  f"({azure_alerts.SEV_LABEL.get(row['severity'], row['severity'])})"
                  for _, row in tbl.iterrows()]
    idx = st.selectbox("Selecciona una regla/alerta para ver el detalle",
                       range(len(opt_labels)),
                       format_func=lambda i: opt_labels[i], key="ac_detail_sel")
    row = tbl.iloc[idx]
    _render_alert_detail(row)


def _safe_str(v) -> str:
    """Convierte un valor a str limpio, tratando NaN/None/'nan' como vacio.

    Clave porque tras un merge de pandas los campos de texto ausentes quedan
    como NaN (float), y llamar .split() sobre un float rompe el render."""
    if v is None:
        return ""
    try:
        if isinstance(v, float) and pd.isna(v):
            return ""
    except Exception:  # noqa: BLE001
        pass
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _render_active_alerts(df_all) -> None:
    """Lista las alertas ACTIVAS (disparadas / Fired) por nombre y permite
    abrir el detalle completo de la seleccionada."""
    st.markdown("**Alertas activas** · en curso ahora mismo (estado Fired)")

    if df_all is None or df_all.empty:
        st.caption("Sin datos.")
        return

    active = df_all[df_all["monitorCondition"].str.lower() == "fired"].copy()
    if active.empty:
        st.success("No hay alertas activas (disparadas) en el alcance y periodo "
                   "seleccionados.")
        return

    # Ordenar por criticidad y luego por disparos.
    active["_rank"] = active["severity"].map(
        lambda s: {"Sev0": 0, "Sev1": 1, "Sev2": 2, "Sev3": 3, "Sev4": 4}.get(s, 9))
    active = active.sort_values(
        ["_rank", "firedTimes"], ascending=[True, False]).reset_index(drop=True)

    st.caption(f"{len(active)} alerta(s) activa(s)")

    # Selector por NOMBRE (incluye recurso y severidad para distinguir).
    def _label(i):
        r = active.iloc[i]
        sev_lbl = azure_alerts.SEV_LABEL.get(r["severity"], r["severity"])
        res = _safe_str(r.get("resource")) or "—"
        try:
            n = int(float(r.get("firedTimes", 0) or 0))
        except Exception:  # noqa: BLE001
            n = 0
        return f"[{sev_lbl}] {_safe_str(r.get('name'))} · {res} · {n} disparos"

    idx = st.selectbox(
        "Selecciona una alerta activa para ver su detalle",
        range(len(active)), format_func=_label, key="ac_active_sel")

    # Detalle de la alerta activa seleccionada.
    _render_alert_detail(active.iloc[idx])


def _render_alert_detail(row) -> None:
    sev = row["severity"]
    lbl, _emo, color, bg = _SEV_UI.get(sev, (row["severityLabel"], "⚪",
                                             "#546e7a", "#eceff1"))
    fired = _safe_str(row.get("monitorCondition")).lower() == "fired"
    try:
        disparos = int(float(row.get("firedTimes", 0) or 0))
    except Exception:  # noqa: BLE001
        disparos = 0
    if fired:
        estado = "Activa (disparada)"
    elif not row.get("enabled", True):
        estado = "Deshabilitada"
    else:
        estado = "Habilitada (sin disparos)"

    with st.container(border=True):
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:.6rem'>"
            f"<span style='background:{bg};color:{color};padding:3px 10px;"
            f"border-radius:6px;font-weight:800'>{lbl}</span>"
            f"<span style='font-weight:800;font-size:1.05rem'>{_safe_str(row.get('name'))}</span>"
            f"</div>", unsafe_allow_html=True)

        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**Información general**")
            st.markdown(
                f"- Estado: {estado}\n"
                f"- Severidad: {lbl}\n"
                f"- Tipo de alerta: {_safe_str(row.get('ruleKind')) or _safe_str(row.get('signalType')) or '—'}\n"
                f"- Regla: {_safe_str(row.get('alertRule')) or _safe_str(row.get('name'))}\n"
                f"- Disparos en el periodo: {disparos}\n"
                f"- ID: `{_safe_str(row.get('alertId'))[-40:] or '—'}`")
            desc = _safe_str(row.get("description"))
            if desc:
                st.caption(desc)
        with d2:
            st.markdown("**Recurso afectado**")
            st.markdown(
                f"- Nombre: {_safe_str(row.get('resource')) or '—'}\n"
                f"- Tipo: {_safe_str(row.get('resourceType')) or '—'}\n"
                f"- Resource Group: {_safe_str(row.get('resourceGroup')) or '—'}\n"
                f"- Suscripción: {_safe_str(row.get('subscriptionName')) or '—'}")

        # Condicion real de la regla.
        st.markdown("**Condición de disparo**")
        cond = _safe_str(row.get("condition"))
        if cond:
            st.code(cond, language=None)
        else:
            st.caption("Esta regla no expone una condición legible por API "
                       "(o es de tipo Activity log sin umbral).")

        # Action Groups.
        st.markdown("**Acciones (Action Groups)**")
        actions = _safe_str(row.get("actions"))
        if actions:
            st.markdown("- " + "\n- ".join(
                a.strip() for a in actions.split(",") if a.strip()))
        else:
            st.caption("Sin Action Groups asociados a la regla.")

        # Timeline — claro segun haya o no disparos.
        st.markdown("**Timeline**")
        if fired or disparos > 0:
            dur = _safe_str(row.get("duration"))
            st.markdown(
                f"Primer disparo: `{_safe_str(row.get('firstFired')) or '—'}`  →  "
                f"Último disparo: `{_safe_str(row.get('lastFired')) or '—'}` "
                f"({_safe_str(row.get('lastFiredAgo')) or '—'})  →  "
                f"Estado actual: **{estado}**"
                + (f"  ·  Duración activa: {dur}" if dur else ""))
        else:
            st.markdown(
                f"Regla **{estado.lower()}**. Sin disparos registrados en el "
                "periodo seleccionado; no hay línea de tiempo de activación.")


# ===========================================================================
# 1.2 Costos (Azure Cost Management + Advisor) — datos reales
# ===========================================================================
@st.cache_data(ttl=600, show_spinner=False)
def _cached_costs(_cred, subs_key: str, subs: list, days: int):
    """Costos reales (cacheados 10 min): diario, por servicio y ahorro."""
    return azure_costs.load_costs(_cred, subs, days)


def _money(v, cur: str = "") -> str:
    """Formatea un monto de forma compacta: 48600 -> '$48,6K'."""
    try:
        v = float(v or 0)
    except Exception:  # noqa: BLE001
        v = 0.0
    sym = "$"
    if abs(v) >= 1_000_000:
        s = f"{sym}{v/1_000_000:.1f}M"
    elif abs(v) >= 1_000:
        s = f"{sym}{v/1_000:.1f}K"
    else:
        s = f"{sym}{v:.0f}"
    s = s.replace(".", ",")
    return s + (f" {cur}" if cur else "")


def render_azure_costs() -> None:
    """1.2 Costos — resumen de gasto, tendencia, servicios y ahorro."""
    cred = _azure_credential()
    if cred is None:
        st.warning("Sesión de Azure no disponible. Reconecta en el login.")
        return

    tenant_id = _azure_selected_tenant_id()

    st.markdown(
        "<div style='display:flex;align-items:baseline;gap:.6rem;margin:.2rem 0 .1rem'>"
        "<span style='font-size:1.25rem;font-weight:800;color:#0050A0'>Resumen de costos</span>"
        "<span style='font-size:.85rem;color:#8894a6'>Entiende dónde se está "
        "gastando, qué cambió y dónde puedes ahorrar primero</span></div>",
        unsafe_allow_html=True)

    subs = _cached_subscriptions(cred, tenant_id or "")
    if not subs:
        st.info("No se encontraron suscripciones accesibles en el tenant "
                "seleccionado. Puede requerir un inicio de sesión adicional (MFA).")
        _azure_tenant_login_flow(tenant_id)
        return
    cred = _azure_credential()

    # Filtros.
    f1, f2, f3 = st.columns([2.4, 1.1, 0.8])
    sub_names = [s["displayName"] or s["subscriptionId"] for s in subs]
    sub_opts = [f"Todas las suscripciones ({len(subs)})"] + sub_names
    sub_choice = f1.selectbox("Suscripción", sub_opts, key="cost_sub")
    period = f2.selectbox("Periodo", ["Últimos 30 días", "Últimos 7 días",
                                      "Últimos 90 días"], key="cost_period")
    with f3:
        st.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        do_refresh = st.button("Actualizar", key="cost_refresh",
                               use_container_width=True)

    days_map = {"Últimos 7 días": 7, "Últimos 30 días": 30, "Últimos 90 días": 90}
    days = days_map[period]
    single = None if sub_choice.startswith("Todas las suscripciones") else sub_choice
    chosen = [s for s in subs
              if single is None or (s["displayName"] or s["subscriptionId"]) == single]
    subs_key = "|".join(sorted(s["subscriptionId"] for s in chosen)) + f"@{days}"

    if do_refresh:
        _cached_costs.clear()

    with st.spinner("Consultando Azure Cost Management…"):
        data = _cached_costs(cred, subs_key, chosen, days)

    fetched = data.get("fetched_at", "")
    cost_status = data.get("cost_status", "error")
    adv_status = data.get("advisor_status", "error")
    errors = data.get("errors") or []
    scanned = data.get("subscriptions_scanned", len(chosen))
    cur = data.get("currency", "")

    s1, s2 = st.columns(2)
    s1.markdown(
        f"<span style='font-size:.78rem'>Cost Management: "
        f"<b style='color:{'#1a7a4a' if cost_status=='ok' else '#c0392b'}'>"
        f"{'actualizado' if cost_status=='ok' else 'no disponible'}</b></span>",
        unsafe_allow_html=True)
    s2.markdown(
        f"<span style='font-size:.78rem'>Advisor: "
        f"<b style='color:{'#1a7a4a' if adv_status=='ok' else '#c0392b'}'>"
        f"{'actualizado' if adv_status=='ok' else 'no disponible'}</b></span>",
        unsafe_allow_html=True)
    st.caption(f"Suscripciones consultadas: {scanned} · Última sincronización: {fetched}")
    if errors:
        with st.expander(f"Avisos de sincronización ({len(errors)})"):
            for e in errors:
                st.text(f"- {e}")

    k = azure_costs.kpis(data)

    # ── KPIs ─────────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    _kpi_card(c1, "Costo acumulado", _money(k["acumulado"], cur),
              f"últimos {days} días", "#0050A0")
    _kpi_card(c2, "Proyección mensual", _money(k["proyeccion"], cur),
              "estimada (prom. diario × 30)", "#2b6cb0")
    var = k["variacion_pct"]
    var_txt = (f"{'▲' if var >= 0 else '▼'} {abs(var)}%" if var is not None else "N/D")
    _kpi_card(c3, "Variación", var_txt, "mitad reciente vs. anterior",
              "#c05621" if (var or 0) > 0 else "#1a7a4a",
              strong=(var or 0) > 10)
    _kpi_card(c4, "Ahorro potencial", _money(k["ahorro"], cur),
              "recomendaciones de Advisor", "#1a7a4a")
    _kpi_card(c5, "Recursos con optimización", k["optimizaciones"],
              "con ahorro sugerido", "#5b21b6")

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)

    # ── Tendencia + Costo por servicio + Dónde ahorrar ───────────────────────
    col_t, col_s, col_a = st.columns(3)
    with col_t:
        st.markdown("**Tendencia de consumo** · costo diario")
        tr = azure_costs.daily_trend(data.get("daily"), last_days=min(days, 10))
        if tr.empty:
            st.caption("Sin datos de costo diario.")
        else:
            maxv = float(tr["amount"].max()) or 1
            for _, r in tr.iterrows():
                pct = r["amount"] / maxv * 100
                st.markdown(
                    f"""
                    <div style='display:flex;align-items:center;gap:.5rem;
                                margin-bottom:.3rem;font-size:.8rem'>
                      <span style='width:76px;color:#6b7280'>{r['date'][5:]}</span>
                      <div style='flex:1;background:#eef1f6;border-radius:5px;height:10px'>
                        <div style='width:{pct}%;background:#2b7fff;height:10px;
                                    border-radius:5px'></div>
                      </div>
                      <span style='width:72px;text-align:right;font-weight:700'>
                        {_money(r['amount'], cur)}</span>
                    </div>
                    """, unsafe_allow_html=True)
    with col_s:
        st.markdown("**Costo por servicio** · principales contribuyentes")
        sv = azure_costs.services_top(data.get("services"), top=6)
        if sv.empty:
            st.caption("Sin datos por servicio.")
        else:
            maxv = float(sv["amount"].max()) or 1
            for _, r in sv.iterrows():
                pct = r["amount"] / maxv * 100
                st.markdown(
                    f"""
                    <div style='margin-bottom:.3rem;font-size:.8rem'>
                      <div style='display:flex;justify-content:space-between'>
                        <span style='font-weight:600'>{r['service']}</span>
                        <span style='font-weight:700'>{_money(r['amount'], cur)}</span>
                      </div>
                      <div style='background:#eef1f6;border-radius:5px;height:9px'>
                        <div style='width:{pct}%;background:#2b7fff;height:9px;
                                    border-radius:5px'></div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
    with col_a:
        st.markdown("**Dónde ahorrar primero** · impacto estimado")
        sav = azure_costs.savings_opportunities(data.get("advisor"), top=5)
        if sav.empty:
            st.caption("Sin recomendaciones de ahorro de Advisor.")
        else:
            imp_color = {"Alto": "#1a7a4a", "Medio": "#c05621", "Bajo": "#6b7280"}
            for _, r in sav.iterrows():
                ic = imp_color.get(r["impact"], "#6b7280")
                st.markdown(
                    f"""
                    <div style='border:1px solid #eef1f6;border-radius:10px;
                                padding:.45rem .6rem;margin-bottom:.35rem;
                                display:flex;justify-content:space-between;
                                align-items:center'>
                      <div>
                        <div style='font-weight:700;font-size:.84rem'>{_safe_str(r['name'])[:42]}</div>
                        <div style='font-size:.72rem;color:#8894a6'>
                          {int(r['recursos'])} recursos · {_money(r['savings'], cur)}</div>
                      </div>
                      <span style='color:{ic};font-weight:800;font-size:.8rem'>{r['impact']}</span>
                    </div>
                    """, unsafe_allow_html=True)

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)

    # ── Tablas + export ──────────────────────────────────────────────────────
    import io as _io
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tab_s, tab_a = st.tabs(["Costo por servicio", "Oportunidades de ahorro"])
    with tab_s:
        sv_all = (data.get("services") if data.get("services") is not None
                  else pd.DataFrame())
        if sv_all.empty:
            st.caption("Sin datos.")
        else:
            agg = (sv_all.groupby("service", as_index=False)["amount"].sum()
                         .sort_values("amount", ascending=False))
            agg["amount"] = agg["amount"].round(2)
            st.dataframe(agg.rename(columns={"service": "Servicio",
                                             "amount": f"Costo ({cur or '—'})"}),
                         use_container_width=True, hide_index=True)
            buf = _io.StringIO(); agg.to_csv(buf, index=False)
            st.download_button("Descargar costo por servicio (CSV)",
                               buf.getvalue().encode("utf-8-sig"),
                               f"costos_servicio_{ts}.csv", "text/csv",
                               key="cost_csv_svc")
    with tab_a:
        adv_all = azure_costs.savings_opportunities(data.get("advisor"), top=500)
        if adv_all.empty:
            st.caption("Sin recomendaciones de ahorro.")
        else:
            v = adv_all.rename(columns={
                "name": "Recomendación", "impact": "Impacto",
                "recursos": "Recursos", "savings": f"Ahorro ({cur or '—'})"})[
                ["Recomendación", "Impacto", "Recursos", f"Ahorro ({cur or '—'})"]]
            st.dataframe(v, use_container_width=True, hide_index=True)
            buf = _io.StringIO(); adv_all.to_csv(buf, index=False)
            st.download_button("Descargar oportunidades de ahorro (CSV)",
                               buf.getvalue().encode("utf-8-sig"),
                               f"costos_ahorro_{ts}.csv", "text/csv",
                               key="cost_csv_sav")

    st.caption("Fuente: Azure Cost Management (gasto real) y Azure Advisor "
               "(ahorro estimado). Proyección y variación se calculan sobre el "
               "gasto real del periodo; no se usan presupuestos externos.")


# ===========================================================================
# 1.3 Azure Security Center (Defender + Advisor) — datos reales
# ===========================================================================
_SEC_SEV_UI = {
    "Crítica":     ("🔴", "#c0392b", "#fdecea"),
    "Alta":        ("🟠", "#c05621", "#fff1e6"),
    "Media":       ("🟡", "#b7791f", "#fff8e1"),
    "Informativa": ("🔵", "#2b6cb0", "#e6f0fb"),
}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_security(_cred, subs_key: str, subs: list):
    """Postura de seguridad real (cacheada 5 min): Defender + Advisor."""
    return azure_security.load_security(_cred, subs)


def render_azure_security() -> None:
    """1.3 Azure Security Center — postura, recomendaciones y recursos."""
    cred = _azure_credential()
    if cred is None:
        st.warning("Sesión de Azure no disponible. Reconecta en el login.")
        return

    tenant_id = _azure_selected_tenant_id()

    st.markdown(
        "<div style='display:flex;align-items:baseline;gap:.6rem;margin:.2rem 0 .1rem'>"
        "<span style='font-size:1.25rem;font-weight:800;color:#0050A0'>Azure Security Center</span>"
        "<span style='font-size:.85rem;color:#8894a6'>Postura de seguridad · "
        "recomendaciones de Advisor y Defender</span></div>", unsafe_allow_html=True)

    subs = _cached_subscriptions(cred, tenant_id or "")
    if not subs:
        st.info("No se encontraron suscripciones accesibles en el tenant "
                "seleccionado. Puede requerir un inicio de sesión adicional (MFA).")
        _azure_tenant_login_flow(tenant_id)
        return
    cred = _azure_credential()

    # Filtros de cabecera.
    f1, f2, f3, f4, f5 = st.columns([1.8, 1.2, 1.1, 1.1, 0.7])
    sub_names = [s["displayName"] or s["subscriptionId"] for s in subs]
    sub_opts = [f"Todas las suscripciones ({len(subs)})"] + sub_names
    sub_choice = f1.selectbox("Suscripción", sub_opts, key="sec_sub")
    origin_f = f2.selectbox("Origen", ["Todos", "Defender", "Advisor"], key="sec_origin")
    sev_f = f3.selectbox("Severidad", ["Todas"] + azure_security.SEV_LABELS,
                         key="sec_sev")
    with f4:
        st.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        do_refresh = st.button("Actualizar", key="sec_refresh",
                               use_container_width=True)

    single = None if sub_choice.startswith("Todas las suscripciones") else sub_choice
    chosen = [s for s in subs
              if single is None or (s["displayName"] or s["subscriptionId"]) == single]
    subs_key = "|".join(sorted(s["subscriptionId"] for s in chosen))

    if do_refresh:
        _cached_security.clear()

    with st.spinner("Consultando Defender for Cloud y Azure Advisor…"):
        df_full = _cached_security(cred, subs_key, chosen)

    # Estado de fuentes (no romper si una falla).
    fetched = df_full.attrs.get("fetched_at", "")
    def_status = df_full.attrs.get("defender_status", "error")
    adv_status = df_full.attrs.get("advisor_status", "error")
    errors = df_full.attrs.get("errors") or []
    scanned = df_full.attrs.get("subscriptions_scanned", len(chosen))

    with f5:
        st.markdown("<div style='height:.2rem'></div>", unsafe_allow_html=True)
    s1, s2 = st.columns(2)
    s1.markdown(
        f"<span style='font-size:.78rem'>Defender: "
        f"<b style='color:{'#1a7a4a' if def_status=='ok' else '#c0392b'}'>"
        f"{'actualizado' if def_status=='ok' else 'no disponible'}</b></span>",
        unsafe_allow_html=True)
    s2.markdown(
        f"<span style='font-size:.78rem'>Advisor: "
        f"<b style='color:{'#1a7a4a' if adv_status=='ok' else '#c0392b'}'>"
        f"{'actualizado' if adv_status=='ok' else 'no disponible'}</b></span>",
        unsafe_allow_html=True)
    st.caption(f"Suscripciones consultadas: {scanned} · Última sincronización: {fetched}")
    if errors:
        with st.expander(f"Avisos de sincronización ({len(errors)})"):
            for e in errors:
                st.text(f"- {e}")

    # Aplicar filtros globales (origen/severidad) a una copia de trabajo.
    df = df_full.copy()
    if origin_f != "Todos":
        df = df[df["origin"] == origin_f]
    if sev_f != "Todas":
        df = df[df["severity"] == sev_f]
    # Conservar attrs tras el filtrado.
    for a in ("secure_score", "healthy_total", "defender_status",
              "advisor_status", "fetched_at"):
        df.attrs[a] = df_full.attrs.get(a)

    k = azure_security.kpis(df)

    # ── Resumen de seguridad: 6 KPIs ─────────────────────────────────────────
    st.markdown("**Resumen de seguridad** · prioriza qué corregir, por qué "
                "importa y qué recursos están afectados")
    r1 = st.columns(3)
    score = k["secure_score"]
    _kpi_card(r1[0], "Postura de seguridad",
              f"{score}/100" if score is not None else "N/D",
              "Secure Score (Defender)", "#0050A0")
    _kpi_card(r1[1], "Críticas", k["criticas"], "Requieren atención inmediata",
              "#c0392b", strong=k["criticas"] > 0)
    _kpi_card(r1[2], "Alta prioridad", k["altas"],
              f"{k['altas_recursos']} recursos afectados", "#c05621",
              strong=k["altas"] > 0)
    r2 = st.columns(3)
    _kpi_card(r2[0], "Recomendaciones", k["recomendaciones"], "Advisor + Defender",
              "#2b6cb0")
    _kpi_card(r2[1], "Recursos afectados", k["recursos"],
              "con hallazgos pendientes", "#5b21b6")
    _kpi_card(r2[2], "Corregidas", k["corregidas"],
              "assessments en estado Healthy", "#1a7a4a")

    if df is None or df.empty:
        st.success("No hay recomendaciones de seguridad pendientes en el alcance "
                   "y filtros seleccionados.")
        return

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)

    # ── Hallazgos por severidad + Origen ─────────────────────────────────────
    col_sev, col_org = st.columns([1.3, 1])
    with col_sev:
        st.markdown("**Hallazgos por severidad**")
        bs = azure_security.by_severity(df)
        if bs.empty:
            st.caption("Sin datos.")
        else:
            for _, r in bs.iterrows():
                _emo, color, _bg = _SEC_SEV_UI.get(r["severity"],
                                                   ("⚪", "#546e7a", "#eceff1"))
                pct = r["pct"]
                st.markdown(
                    f"""
                    <div style='margin-bottom:.4rem'>
                      <div style='display:flex;justify-content:space-between;
                                  font-size:.8rem;margin-bottom:2px'>
                        <span style='font-weight:700;color:{color}'>{r['severity']}</span>
                        <span style='color:#6b7280'>{int(r['count'])} · {pct}%</span>
                      </div>
                      <div style='background:#eef1f6;border-radius:6px;height:12px'>
                        <div style='width:{pct}%;background:{color};height:12px;
                                    border-radius:6px'></div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
    with col_org:
        st.markdown("**Origen de recomendaciones**")
        bo = azure_security.by_origin(df)
        if bo.empty:
            st.caption("Sin datos.")
        else:
            for _, r in bo.iterrows():
                st.markdown(
                    f"""
                    <div style='border:1px solid #eef1f6;border-radius:10px;
                                padding:.5rem .7rem;margin-bottom:.4rem'>
                      <div style='display:flex;justify-content:space-between'>
                        <span style='font-weight:700'>{r['origin']}</span>
                        <span style='font-weight:800;color:#0050A0'>{int(r['pct'])}%</span>
                      </div>
                      <div style='font-size:.74rem;color:#8894a6'>
                        {int(r['count'])} recomendaciones · {int(r['recursos'])} recursos</div>
                    </div>
                    """, unsafe_allow_html=True)

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Recursos con mayor exposición (por tipo) ─────────────────────────────
    st.markdown("**Recursos con mayor exposición** · por tipo de recurso")
    tt = azure_security.top_resource_types(df, top=8)
    if not tt.empty:
        maxv = int(tt["recursos"].max())
        _hbar_list(tt.to_dict("records"), "resourceType", "recursos", maxv,
                   color="#c05621")

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Prioridad de corrección (top recomendaciones) ────────────────────────
    st.markdown("**Prioridad de corrección** · ¿dónde actuar primero?")
    prio = azure_security.priority_recommendations(df, top=10)
    if not prio.empty:
        for _, r in prio.iterrows():
            _emo, color, _bg = _SEC_SEV_UI.get(r["severity"],
                                               ("⚪", "#546e7a", "#eceff1"))
            with st.container(border=True):
                a, b, c = st.columns([3.2, 1.3, 1])
                a.markdown(
                    f"<div style='font-weight:700'>{_safe_str(r['name'])}</div>"
                    f"<div style='font-size:.76rem;color:#8894a6'>"
                    f"Origen: {r['origin']} · {_safe_str(r['subscriptionName'])}</div>",
                    unsafe_allow_html=True)
                b.markdown(
                    f"<span style='color:{color};font-weight:800'>{r['severity']}</span>",
                    unsafe_allow_html=True)
                c.markdown(
                    f"<div style='font-size:1.2rem;font-weight:800'>{int(r['recursos'])}</div>"
                    f"<div style='font-size:.72rem;color:#8894a6'>recursos</div>",
                    unsafe_allow_html=True)

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Tabla principal: recomendaciones prioritarias ────────────────────────
    st.markdown("**Recomendaciones prioritarias**")
    tf1, tf2 = st.columns([1.3, 2])
    tipo_opts = ["Todos"] + azure_security.resource_types(df)
    tipo_f = tf1.selectbox("Tipo de recurso", tipo_opts, key="sec_type")
    search = tf2.text_input("Buscar recomendación, recurso, RG, suscripción",
                            key="sec_search")

    tbl = df.copy()
    if tipo_f != "Todos":
        tbl = tbl[tbl["resourceType"] == tipo_f]
    if search:
        q = search.lower()
        mask = (tbl["name"].str.lower().str.contains(q, na=False)
                | tbl["resource"].str.lower().str.contains(q, na=False)
                | tbl["resourceGroup"].str.lower().str.contains(q, na=False)
                | tbl["subscriptionName"].str.lower().str.contains(q, na=False))
        tbl = tbl[mask]

    # Agregado por recomendacion para la tabla (nombre + nº recursos).
    rec_table = azure_security.priority_recommendations(tbl, top=500)
    view = rec_table[["name", "origin", "severity", "recursos",
                      "subscriptionName", "lastFound"]].rename(columns={
        "name": "Recomendación", "origin": "Origen", "severity": "Severidad",
        "recursos": "Recursos", "subscriptionName": "Suscripción",
        "lastFound": "Último hallazgo"})
    st.dataframe(view, use_container_width=True, hide_index=True, height=360)
    st.caption(f"Total recomendaciones: {len(rec_table)} · "
               f"{len(tbl)} hallazgos (recurso×recomendación)")

    # Exportar (respeta filtros).
    import io as _io
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    e1, e2 = st.columns(2)
    buf_rec = _io.StringIO()
    rec_table.to_csv(buf_rec, index=False)
    e1.download_button("Exportar recomendaciones (CSV)",
                       buf_rec.getvalue().encode("utf-8-sig"),
                       f"seguridad_recomendaciones_{ts}.csv", "text/csv",
                       key="sec_csv_rec")
    aff_all = azure_security.affected_resources(tbl)
    buf_aff = _io.StringIO()
    aff_all.to_csv(buf_aff, index=False)
    e2.download_button("Exportar recursos afectados (CSV)",
                       buf_aff.getvalue().encode("utf-8-sig"),
                       f"seguridad_recursos_{ts}.csv", "text/csv",
                       key="sec_csv_aff")

    # ── Detalle de recomendación ─────────────────────────────────────────────
    st.markdown("**Detalle de recomendación**")
    if not rec_table.empty:
        labels = [f"[{r['severity']}] {_safe_str(r['name'])} "
                  f"({r['origin']}, {int(r['recursos'])} recursos)"
                  for _, r in rec_table.iterrows()]
        idx = st.selectbox("Selecciona una recomendación",
                           range(len(labels)), format_func=lambda i: labels[i],
                           key="sec_rec_sel")
        _render_security_rec_detail(rec_table.iloc[idx], tbl)

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Recursos afectados ───────────────────────────────────────────────────
    st.markdown("**Recursos afectados** · solo con recomendaciones pendientes")
    aff = azure_security.affected_resources(tbl)
    if aff.empty:
        st.caption("Sin recursos afectados con los filtros actuales.")
    else:
        view_aff = aff[["resource", "resourceType", "subscriptionName",
                        "resourceGroup", "riesgo", "hallazgos"]].rename(columns={
            "resource": "Recurso", "resourceType": "Tipo",
            "subscriptionName": "Suscripción", "resourceGroup": "Resource Group",
            "riesgo": "Riesgo", "hallazgos": "Hallazgos"})
        st.dataframe(view_aff, use_container_width=True, hide_index=True, height=320)
        st.caption(f"Total recursos afectados: {len(aff)}")

        # Detalle de recurso.
        res_labels = [f"[{r['riesgo']}] {r['resource']} · {r['resourceType']} "
                      f"({int(r['hallazgos'])} hallazgos)"
                      for _, r in aff.iterrows()]
        ridx = st.selectbox("Ver detalle de un recurso afectado",
                            range(len(res_labels)),
                            format_func=lambda i: res_labels[i], key="sec_res_sel")
        _render_security_resource_detail(aff.iloc[ridx], tbl)


def _render_security_rec_detail(rec, df) -> None:
    """Detalle de una recomendación: descripción, remediación y recursos."""
    _emo, color, bg = _SEC_SEV_UI.get(rec["severity"], ("⚪", "#546e7a", "#eceff1"))
    with st.container(border=True):
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:.6rem'>"
            f"<span style='background:{bg};color:{color};padding:3px 10px;"
            f"border-radius:6px;font-weight:800'>{rec['severity']}</span>"
            f"<span style='font-weight:800;font-size:1.02rem'>{_safe_str(rec['name'])}</span>"
            f"</div>", unsafe_allow_html=True)
        st.markdown(f"- Origen: **{rec['origin']}**\n"
                    f"- Recursos afectados: **{int(rec['recursos'])}**\n"
                    f"- Suscripción: {_safe_str(rec['subscriptionName'])}")
        rem = _safe_str(rec.get("remediation"))
        if rem:
            st.markdown("**Qué aplicar (remediación)**")
            st.info(rem)
        # Recursos afectados por esta recomendación.
        affected = df[df["name"] == rec["name"]]
        if not affected.empty:
            st.markdown("**Recursos afectados por esta recomendación**")
            cols = ["resource", "resourceType", "subscriptionName",
                    "resourceGroup", "resourceId"]
            v = affected[cols].drop_duplicates().rename(columns={
                "resource": "Recurso", "resourceType": "Tipo",
                "subscriptionName": "Suscripción",
                "resourceGroup": "Resource Group", "resourceId": "Resource ID"})
            st.dataframe(v, use_container_width=True, hide_index=True)
        ref = _safe_str(rec.get("assessmentId"))
        if ref:
            st.caption(f"Referencia (ID): {ref[-60:]}")


def _render_security_resource_detail(res, df) -> None:
    """Detalle de un recurso: info + todas sus recomendaciones."""
    with st.container(border=True):
        st.markdown(f"**{_safe_str(res['resource'])}** · {_safe_str(res['resourceType'])}")
        c1, c2 = st.columns(2)
        c1.markdown(
            f"- Tipo: {_safe_str(res['resourceType']) or '—'}\n"
            f"- Suscripción: {_safe_str(res['subscriptionName'])}\n"
            f"- Resource Group: {_safe_str(res['resourceGroup']) or '—'}")
        c2.markdown(
            f"- Riesgo máximo: **{res['riesgo']}**\n"
            f"- Recomendaciones: **{int(res['hallazgos'])}**\n"
            f"- Resource ID: `{_safe_str(res['resourceId'])[-50:]}`")
        recs = df[df["resourceId"] == res["resourceId"]]
        if not recs.empty:
            st.markdown("**Recomendaciones de este recurso**")
            v = recs[["name", "origin", "severity", "remediation"]].rename(columns={
                "name": "Recomendación", "origin": "Origen",
                "severity": "Severidad", "remediation": "Qué aplicar"})
            st.dataframe(v, use_container_width=True, hide_index=True)


# ===========================================================================
# 1.4 Inventario de recursos (Azure Resource Graph) — datos reales
# ===========================================================================
@st.cache_data(ttl=300, show_spinner=False)
def _cached_inventory(_cred, subs_key: str, subs: list):
    """Inventario completo de recursos (cacheado 5 min) via Resource Graph."""
    return azure_inventory.list_resources(_cred, subs)


def _hbar_list(rows, label_key, value_key, max_val, color="#2b7fff",
               sub_key=None):
    """Dibuja una lista de barras horizontales simples (tipo mockup)."""
    for r in rows:
        val = int(r[value_key])
        pct = (val / max_val * 100) if max_val else 0
        sub = f"<div style='font-size:.72rem;color:#8894a6'>{r[sub_key]}</div>" \
            if sub_key and r.get(sub_key) else ""
        st.markdown(
            f"""
            <div style='margin-bottom:.45rem'>
              <div style='display:flex;justify-content:space-between;
                          font-size:.82rem;margin-bottom:2px'>
                <span style='font-weight:600;color:#1A1A2E'>{r[label_key]}</span>
                <span style='font-weight:700;color:#1A1A2E'>{val}</span>
              </div>
              {sub}
              <div style='background:#eef1f6;border-radius:6px;height:10px'>
                <div style='width:{pct}%;background:{color};height:10px;
                            border-radius:6px'></div>
              </div>
            </div>
            """, unsafe_allow_html=True)


def render_azure_inventory() -> None:
    """1.4 Inventario — resumen y tabla operativa de todos los recursos Azure."""
    cred = _azure_credential()
    if cred is None:
        st.warning("Sesión de Azure no disponible. Reconecta en el login.")
        return

    tenant_id = _azure_selected_tenant_id()

    st.markdown(
        "<div style='display:flex;align-items:baseline;gap:.6rem;margin:.2rem 0 .1rem'>"
        "<span style='font-size:1.25rem;font-weight:800;color:#0050A0'>Resumen del inventario</span>"
        "<span style='font-size:.85rem;color:#8894a6'>Visión general de recursos Azure · "
        "qué tenemos, dónde está y qué apareció recientemente</span></div>",
        unsafe_allow_html=True)

    subs = _cached_subscriptions(cred, tenant_id or "")
    if not subs:
        st.info("No se encontraron suscripciones accesibles en el tenant "
                "seleccionado. Puede requerir un inicio de sesión adicional (MFA).")
        _azure_tenant_login_flow(tenant_id)
        return
    cred = _azure_credential()

    # Filtros de cabecera.
    f1, f2 = st.columns([3.0, 0.8])
    sub_names = [s["displayName"] or s["subscriptionId"] for s in subs]
    sub_opts = [f"Todas las suscripciones ({len(subs)})"] + sub_names
    sub_choice = f1.selectbox("Suscripción", sub_opts, key="inv_sub_one")
    with f2:
        st.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        do_refresh = st.button("Actualizar", key="inv_refresh",
                               use_container_width=True)

    single = None if sub_choice.startswith("Todas las suscripciones") else sub_choice
    chosen = [s for s in subs
              if single is None or (s["displayName"] or s["subscriptionId"]) == single]
    subs_key = "|".join(sorted(s["subscriptionId"] for s in chosen))

    if do_refresh:
        _cached_inventory.clear()

    with st.spinner("Consultando Azure Resource Graph…"):
        df = _cached_inventory(cred, subs_key, chosen)

    fetched = df.attrs.get("fetched_at", "")
    errors = df.attrs.get("errors") or []
    scanned = df.attrs.get("subscriptions_scanned", len(chosen))
    st.caption(f"Suscripciones consultadas: {scanned} · Última consulta: {fetched}")
    if errors:
        with st.expander(f"Advertencia: {len(errors)} aviso(s) al consultar"):
            for e in errors:
                st.text(f"- {e}")

    if df is None or df.empty:
        st.success("No se encontraron recursos en el alcance seleccionado.")
        return

    k = azure_inventory.kpis(df)

    # ── Tarjetas KPI (dos filas de 3) ────────────────────────────────────────
    r1 = st.columns(3)
    _kpi_card(r1[0], "Total de recursos", f"{k['total']:,}".replace(",", "."),
              "en el alcance seleccionado", "#0050A0")
    _kpi_card(r1[1], "Recursos nuevos", k["nuevos"], "creados en los últimos 7 días",
              "#1a7a4a")
    _kpi_card(r1[2], "Suscripciones", k["subs"], "con recursos activos", "#2b6cb0")
    r2 = st.columns(3)
    _kpi_card(r2[0], "Resource Groups", k["resourceGroups"], "contenedores lógicos",
              "#0050A0")
    _kpi_card(r2[1], "Regiones utilizadas", k["regiones"], "distribución global",
              "#2b6cb0")
    _kpi_card(r2[2], "Tipos de recursos", k["tipos"],
              f"{k['categorias']} categorías", "#5b21b6")

    st.markdown("<div style='height:.6rem'></div>", unsafe_allow_html=True)

    # ── Recursos por tipo + por suscripción ──────────────────────────────────
    col_t, col_s = st.columns(2)
    with col_t:
        st.markdown("**Recursos por tipo** · top 8 del inventario actual")
        bt = azure_inventory.by_type(df, top=8)
        if bt.empty:
            st.caption("Sin datos.")
        else:
            maxv = int(bt["count"].max())
            _hbar_list(bt.to_dict("records"), "typePretty", "count", maxv)
    with col_s:
        st.markdown("**Recursos por suscripción** · distribución del inventario")
        bs = azure_inventory.by_subscription(df)
        if bs.empty:
            st.caption("Sin datos.")
        else:
            for _, r in bs.iterrows():
                nuevos = (f" · +{int(r['nuevos'])} esta semana"
                          if r["nuevos"] else "")
                st.markdown(
                    f"""
                    <div style='border:1px solid #eef1f6;border-radius:10px;
                                padding:.5rem .7rem;margin-bottom:.4rem;
                                display:flex;justify-content:space-between;
                                align-items:center'>
                      <div>
                        <div style='font-weight:700'>{r['subscriptionName']}</div>
                        <div style='font-size:.74rem;color:#8894a6'>
                          {int(r['count'])} recursos{nuevos}</div>
                      </div>
                      <div style='font-size:1.1rem;font-weight:800;color:#0050A0'>
                        {int(r['pct'])}%</div>
                    </div>
                    """, unsafe_allow_html=True)

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Distribución por región ──────────────────────────────────────────────
    st.markdown("**Distribución por región** · recursos desplegados")
    br = azure_inventory.by_region(df, top=6)
    if not br.empty:
        maxv = int(br["count"].max())
        _hbar_list(br.to_dict("records"), "locationPretty", "count", maxv)

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

    # ── Inventario completo (todos los recursos), filtrable por tipo ─────────
    import io as _io
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    st.markdown(f"**Inventario completo** · {k['total']:,} recursos"
                .replace(",", "."))

    # Descargar TODO el inventario (sin filtros), prominente.
    full_cols = {
        "name": "Nombre", "type": "Tipo", "typePretty": "Tipo (legible)",
        "category": "Categoría", "subscriptionName": "Suscripción",
        "resourceGroup": "ResourceGroup", "locationPretty": "Región",
        "createdDate": "Creado",
    }
    full_csv = _io.StringIO()
    df[list(full_cols.keys())].rename(columns=full_cols).to_csv(
        full_csv, index=False)
    st.download_button(
        f"⬇ Descargar inventario completo ({k['total']} recursos)",
        full_csv.getvalue().encode("utf-8-sig"),
        f"azure_inventario_completo_{ts}.csv", "text/csv",
        key="inv_csv_full", type="primary")

    # Filtros: tipo EXACTO (todos los tipos presentes) + buscador.
    tf1, tf2 = st.columns([1.4, 2])
    tipos_present = (df.groupby("typePretty").size()
                       .sort_values(ascending=False))
    tipo_opts = ["Todos los tipos"] + [
        f"{t} ({n})" for t, n in tipos_present.items()]
    tipo_label_to_name = {f"{t} ({n})": t for t, n in tipos_present.items()}
    tipo_sel = tf1.selectbox("Tipo de recurso", tipo_opts, key="inv_type")
    search = tf2.text_input("Buscar recurso, RG, tipo…", key="inv_search")

    tbl = df.copy()
    if tipo_sel != "Todos los tipos":
        tbl = tbl[tbl["typePretty"] == tipo_label_to_name.get(tipo_sel, "")]
    if search:
        q = search.lower()
        mask = (tbl["name"].str.lower().str.contains(q, na=False)
                | tbl["resourceGroup"].str.lower().str.contains(q, na=False)
                | tbl["typePretty"].str.lower().str.contains(q, na=False)
                | tbl["type"].str.lower().str.contains(q, na=False))
        tbl = tbl[mask]

    # Tabla ordenada por tipo para que quede agrupada visualmente.
    tbl = tbl.sort_values(["typePretty", "name"]).reset_index(drop=True)
    view_cols = {
        "name": "Nombre", "typePretty": "Tipo", "type": "Proveedor",
        "subscriptionName": "Suscripción", "resourceGroup": "Resource Group",
        "locationPretty": "Región", "createdDate": "Creado",
    }
    show = tbl[list(view_cols.keys())].rename(columns=view_cols)
    st.dataframe(show, use_container_width=True, hide_index=True, height=460)
    st.caption(f"Mostrando {len(tbl)} de {k['total']} recursos")

    # Descargar la vista filtrada.
    buf = _io.StringIO()
    tbl[list(view_cols.keys())].rename(columns=view_cols).to_csv(buf, index=False)
    st.download_button("Descargar vista filtrada (CSV)",
                       buf.getvalue().encode("utf-8-sig"),
                       f"azure_inventario_filtrado_{ts}.csv", "text/csv",
                       key="inv_csv_filtered")


# ===========================================================================
# 1.6 Recursos deprecados (Azure Advisor) — datos reales
# ===========================================================================
_DEP_STATUS_UI = {
    "Retirado":  ("#c0392b", "#fdecea"),
    "En retiro": ("#c05621", "#fff1e6"),
    "Activo":    ("#2b6cb0", "#e6f0fb"),
}


@st.cache_data(ttl=600, show_spinner=False)
def _cached_deprecations(_cred, subs_key: str, subs: list):
    """Recursos deprecados reales (cacheados 10 min)."""
    return azure_deprecations.list_deprecations(_cred, subs)


def render_azure_deprecations() -> None:
    """Wrapper que captura y muestra cualquier error del render de deprecados
    sin romper la app (diagnostico)."""
    try:
        _render_azure_deprecations_impl()
    except Exception as exc:  # noqa: BLE001
        import traceback
        st.error(f"Error al renderizar Recursos deprecados: "
                 f"{type(exc).__name__}: {exc}")
        with st.expander("Detalle técnico"):
            st.code(traceback.format_exc())


def _render_azure_deprecations_impl() -> None:
    """1.6 Recursos deprecados — servicios en proceso de retiro (Advisor)."""
    cred = _azure_credential()
    if cred is None:
        st.warning("Sesión de Azure no disponible. Reconecta en el login.")
        return

    tenant_id = _azure_selected_tenant_id()

    st.markdown(
        "<div style='display:flex;align-items:baseline;gap:.6rem;margin:.2rem 0 .1rem'>"
        "<span style='font-size:1.2rem;font-weight:800;color:#0050A0'>Recursos deprecados</span>"
        "<span style='font-size:.85rem;color:#8894a6'>Servicios con retiro anunciado · "
        "fuente: Azure Advisor</span></div>", unsafe_allow_html=True)

    subs = _cached_subscriptions(cred, tenant_id or "")
    if not subs:
        st.info("No se encontraron suscripciones accesibles en el tenant "
                "seleccionado. Puede requerir un inicio de sesión adicional (MFA).")
        _azure_tenant_login_flow(tenant_id)
        return
    cred = _azure_credential()

    # Filtros de cabecera.
    f1, f2, f3 = st.columns([2.0, 1.4, 0.8])
    sub_names = [s["displayName"] or s["subscriptionId"] for s in subs]
    sub_opts = [f"Todas las suscripciones ({len(subs)})"] + sub_names
    sub_choice = f1.selectbox("Suscripción", sub_opts, key="dep_sub_one")
    estado_f = f2.selectbox("Estado", ["Todos", "Activo", "En retiro", "Retirado"],
                            key="dep_estado")
    with f3:
        st.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
        do_refresh = st.button("Actualizar", key="dep_refresh",
                               use_container_width=True)

    single = None if sub_choice.startswith("Todas las suscripciones") else sub_choice
    chosen = [s for s in subs
              if single is None or (s["displayName"] or s["subscriptionId"]) == single]
    subs_key = "|".join(sorted(s["subscriptionId"] for s in chosen))

    if do_refresh:
        _cached_deprecations.clear()

    with st.spinner("Consultando Azure Advisor…"):
        df = _cached_deprecations(cred, subs_key, chosen)

    fetched = df.attrs.get("fetched_at", "")
    errors = df.attrs.get("errors") or []
    scanned = df.attrs.get("subscriptions_scanned", len(chosen))
    st.caption(f"Suscripciones consultadas: {scanned} · Última consulta: {fetched}")
    if errors:
        # Deduplicar avisos (muchas subs suelen fallar con el mismo motivo).
        reasons = {}
        for e in errors:
            motivo = e.split(": ", 1)[-1].strip()
            reasons[motivo] = reasons.get(motivo, 0) + 1
        # Si Advisor no respondió en ninguna, avisar claro arriba.
        if df.empty and any(
                "advisor" in m.lower() or "no respond" in m.lower()
                for m in reasons):
            st.warning("Azure Advisor no está devolviendo recomendaciones en "
                       "este momento (posible error temporal del servicio o "
                       "Advisor no habilitado). Intenta de nuevo con **Actualizar** "
                       "en unos minutos.")
        with st.expander(f"Detalle de avisos ({len(errors)} suscripción(es))"):
            for motivo, n in sorted(reasons.items(), key=lambda x: -x[1]):
                st.text(f"- {motivo}  (×{n})")

    s = azure_deprecations.summary(df)
    c1, c2, c3, c4 = st.columns(4)
    _kpi_card(c1, "Servicios afectados", s["total"], "con retiro anunciado", "#0050A0")
    _kpi_card(c2, "Activo", s["activo"], "anunciado, sin fecha próxima", "#2b6cb0")
    _kpi_card(c3, "En retiro", s["en_retiro"], "retiro próximo (≤180 d)",
              "#c05621", strong=s["en_retiro"] > 0)
    _kpi_card(c4, "Retirado", s["retirado"], "fecha ya cumplida",
              "#c0392b", strong=s["retirado"] > 0)

    st.markdown("<div style='height:.5rem'></div>", unsafe_allow_html=True)

    if df is None or df.empty:
        st.success("No hay servicios en proceso de retiro en el alcance "
                   "seleccionado según Azure Advisor.")
        return

    # Filtro de estado.
    tbl = df.copy()
    if estado_f != "Todos":
        tbl = tbl[tbl["status"] == estado_f]

    # Buscador (blindado: convertir a str antes de .str.contains).
    search = st.text_input("Buscar (servicio, recurso, acción)", key="dep_search")
    if search:
        q = search.lower()
        mask = (tbl["service"].astype(str).str.lower().str.contains(q, na=False)
                | tbl["resource"].astype(str).str.lower().str.contains(q, na=False)
                | tbl["action"].astype(str).str.lower().str.contains(q, na=False))
        tbl = tbl[mask]

    # ── Servicios Afectados ─────────────────────────────────────────────────
    st.markdown("**Servicios afectados** · en proceso de retiro en el entorno")
    view_cols = {
        "service": "Servicio", "resource": "Recurso / SKU",
        "retireDateRaw": "Fecha de retiro", "status": "Estado",
        "action": "Acción recomendada", "subscriptionName": "Suscripción",
    }
    # Combinar resource + sku en una sola columna legible (blindado contra NaN).
    tbl = tbl.copy()
    tbl["resource"] = tbl.apply(
        lambda r: _safe_str(r.get("resource"))
                  + (f" · {_safe_str(r.get('sku'))}" if _safe_str(r.get("sku")) else ""),
        axis=1)
    show = tbl[list(view_cols.keys())].rename(columns=view_cols)
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption(f"Total servicios afectados: {len(tbl)}")

    # Export.
    import io as _io
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = _io.StringIO()
    tbl.to_csv(buf, index=False)
    st.download_button("Descargar CSV", buf.getvalue().encode("utf-8-sig"),
                       f"azure_deprecados_{ts}.csv", "text/csv", key="dep_csv")

    st.caption("La vista se actualiza según los anuncios oficiales de Microsoft "
               "expuestos por Azure Advisor. No se agregan datos manuales.")


def _render_area_shell(area_key: str, area_label: str, icon: str,
                       description: str) -> None:
    """Cascara generica de un area: una pestaña por nube.

    Nubes conectadas -> contenido real si existe (p. ej. Azure + alertas ->
    Alert Center); si no, tarjeta de 'sesion activa'. No conectadas -> 'Inicia
    sesion primero'. Mismo patron para todas las areas."""
    st.markdown(f"<div class='cops-section'><span class='bar'></span>"
                f"{area_label}</div>", unsafe_allow_html=True)
    st.caption(f"{description} Las pestañas sin sesion activa piden iniciar "
               "sesion primero.")

    connected = set(_connected_clouds())
    tabs = st.tabs([f"  {p}  " for p in _PROVIDERS])
    for tab, provider in zip(tabs, _PROVIDERS):
        with tab:
            if provider not in connected:
                _not_connected_placeholder(provider)
            elif area_key == "alertas" and provider == "Azure":
                render_azure_alert_center()
            elif area_key == "deprecados" and provider == "Azure":
                render_azure_deprecations()
            elif area_key == "inventario" and provider == "Azure":
                render_azure_inventory()
            elif area_key == "seguridad" and provider == "Azure":
                render_azure_security()
            else:
                _connected_placeholder(provider, area_label, icon)


def render_panel_main() -> None:
    render_header()

    area_label = st.radio(
        "Area de gestion",
        _PANEL_LABELS,
        horizontal=True,
        key="panel_area",
        label_visibility="collapsed",
    )

    area = _PANEL_BY_LABEL.get(area_label)
    if area:
        key, label, icon, description = area
        _render_area_shell(key, label, icon, description)


# ===========================================================================
# Main — router de vistas
# ===========================================================================
def render_login_view() -> None:
    render_header()
    render_health_dashboard()
    render_login()


def main() -> None:
    st.session_state.setdefault("view", "login")

    # Reanudar sesion tras un F5 (una sola vez por carga del navegador).
    _restore_session()

    # Si se pierde la conexion (p.ej. tras logout), forzar login.
    if st.session_state["view"] == "panel" and not _connected_clouds():
        st.session_state["view"] = "login"

    if st.session_state["view"] == "panel":
        render_sidebar()
        render_panel_main()
    else:
        render_login_view()


if __name__ == "__main__":
    main()
