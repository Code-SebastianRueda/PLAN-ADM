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
        st.success(msg)
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
            st.success(f"AWS ({cliente}) conectado. "
                       "Las cuentas se listaran en el panel central.")
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
            acct = conn.account_hint or "cuenta Azure"
            st.success(f"Azure conectado como {acct}. "
                       f"{len(conn.tenants)} tenant(s) disponible(s). "
                       "Elige el tenant en el panel central.")
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
        st.selectbox(
            "Tenant",
            options=list(tenant_labels.keys()),
            key="scope_Azure_tenant",
            help="Selecciona el tenant con el que trabajaras.",
        )
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


def _sidebar_aws_scope() -> None:
    """Cuentas/roles REALES del cliente AWS, listados BAJO DEMANDA en el panel
    (igual que Azure con sus tenants). El token se obtuvo en el login."""
    cliente = st.session_state.get("aws_cliente_sel", "")
    conn = st.session_state.get("aws_connection")
    token = getattr(conn, "access_token", "") if conn else ""
    region = getattr(conn, "sso_region", "us-east-1") if conn else "us-east-1"

    with st.expander(f"AWS · {cliente}", expanded=True):
        if not token:
            st.caption("Sin token SSO.")
            return

        with st.spinner("Cargando cuentas…"):
            accounts = _aws_accounts(token, region)
        if not accounts:
            st.caption("No se encontraron cuentas (o el token expiro).")
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
# Panel central — Area principal (vacia por ahora)
# ===========================================================================
def render_panel_main() -> None:
    render_header()
    st.markdown(
        """
        <div style='border:1.5px dashed #cfd8e3; border-radius:14px;
                    padding:3rem 2rem; text-align:center; background:#fbfcfe;
                    color:#8894a6; margin-top:.5rem;'>
            <div style='font-size:2.2rem;'>🗂️</div>
            <div style='font-size:1.1rem;font-weight:700;color:#5b6472;margin-top:.4rem;'>
                Panel central
            </div>
            <div style='font-size:.9rem;margin-top:.3rem;'>
                Area de trabajo lista. El contenido operativo se definira en el proximo modulo.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ===========================================================================
# Main — router de vistas
# ===========================================================================
def render_login_view() -> None:
    render_header()
    render_health_dashboard()
    render_login()


def main() -> None:
    st.session_state.setdefault("view", "login")

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
