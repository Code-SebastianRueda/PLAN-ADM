"""
azure_auth.py
=============
Autenticacion REAL de Azure para PLAN-ADM mediante azure-identity.

Modo soportado en este modulo: INTERACTIVO (navegador).

Alcance de ESTE paso (login):
    - Autenticar UNICAMENTE al usuario (un solo flujo de navegador).
    - LEER la lista de tenants a los que el usuario tiene acceso (solo nombre
      e ID). NO se inicia sesion en ningun tenant concreto ni se enumeran
      suscripciones aqui.
    - La seleccion de un tenant especifico y su login/descubrimiento de
      suscripciones se haran DESPUES, en el panel central.

Nota de diseño: el login interactivo levanta un servidor de redirect local.
Para que conviva bien con Streamlit (que re-ejecuta el script) la autenticacion
se realiza en UNA sola llamada bloqueante dentro de este modulo y el resultado
(credencial + AuthenticationRecord) se guarda para reutilizarlo sin reabrir el
navegador.

No depende de Streamlit: solo logica de autenticacion/lectura, para poder
probarlo de forma aislada. La UI (app.py) consume estas funciones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

try:
    from azure.identity import (
        DeviceCodeCredential,
        TokenCachePersistenceOptions,
    )
    from azure.mgmt.subscription import SubscriptionClient
    from azure.core.exceptions import ClientAuthenticationError
    _AZURE_AVAILABLE = True
except Exception:  # noqa: BLE001
    _AZURE_AVAILABLE = False


# Scope del Azure Resource Manager (plano de control).
_ARM_SCOPE = "https://management.azure.com/.default"


class AzureAuthError(Exception):
    """Error de autenticacion o lectura en Azure."""


@dataclass
class Tenant:
    id: str
    display_name: str = ""
    default_domain: str = ""

    @property
    def name(self) -> str:
        """Nombre amigable del tenant (sin el ID)."""
        return self.display_name or self.default_domain or self.id

    @property
    def label(self) -> str:
        """Etiqueta para selector: prioriza el nombre; el ID queda como apoyo."""
        name = self.name
        if name and name != self.id:
            return name
        return self.id


@dataclass
class AzureConnection:
    credential: object = None
    record: object = None          # AuthenticationRecord para reutilizar la sesion
    tenants: List[Tenant] = field(default_factory=list)
    account_hint: str = ""         # upn / email del usuario autenticado
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.credential is not None and not self.error


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------
def _cache_opts() -> "TokenCachePersistenceOptions":
    # Cache persistente para reutilizar el token entre ejecuciones.
    return TokenCachePersistenceOptions(name="plan-adm-azure")


def _build_credential(prompt_callback=None, record=None,
                      disable_automatic_authentication=False,
                      tenant_id=None):
    """Crea una DeviceCodeCredential (flujo de codigo de dispositivo).

    Device code NO usa servidor de redirect local, por lo que elimina de raiz
    el 'state mismatch' que sufre el flujo de navegador dentro de Streamlit.

    - prompt_callback(verification_uri, user_code, expires_on) -> para mostrar
      el codigo al usuario.
    - additionally_allowed_tenants=["*"] -> la MISMA credencial podra emitir
      tokens para el tenant que se elija luego en el panel, sin reautenticar.
    - authentication_record (si existe)  -> reanuda sesion desde la cache.
    - disable_automatic_authentication=True -> get_token NO abre el device code
      si la cache expiro: lanza AuthenticationRequiredError. Imprescindible para
      la restauracion silenciosa en el arranque (si no, bloquea pidiendo codigo).
    """
    kwargs = dict(
        cache_persistence_options=_cache_opts(),
        additionally_allowed_tenants=["*"],
    )
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    if disable_automatic_authentication:
        kwargs["disable_automatic_authentication"] = True
    if prompt_callback is not None:
        kwargs["prompt_callback"] = prompt_callback
    if record is not None:
        kwargs["authentication_record"] = record
    return DeviceCodeCredential(**kwargs)


# ---------------------------------------------------------------------------
# Login por DEVICE CODE (dos pasos para Streamlit)
# ---------------------------------------------------------------------------
@dataclass
class AzureDeviceAuth:
    """Datos del paso 1 del device code (para mostrar el codigo al usuario)."""
    verification_uri: str = ""
    user_code: str = ""
    message: str = ""
    error: str = ""
    _thread: object = None
    _result: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.user_code) and not self.error


def begin_device_login(tenant_id: Optional[str] = None) -> AzureDeviceAuth:
    """Paso 1: inicia el device code en un hilo y devuelve el codigo a mostrar.

    tenant_id: si se indica, la autenticacion se hace CONTRA ESE TENANT. Esto
    fuerza el flujo de MFA / acceso condicional propio de ese tenant (necesario
    cuando un tenant exige un challenge adicional, p. ej. AADSTS50158). Si es
    None, autentica contra el home tenant del usuario.

    El login real se completa en segundo plano; se consulta con finish_login().
    Nunca lanza: los errores quedan en .error.
    """
    import threading

    da = AzureDeviceAuth()
    if not _AZURE_AVAILABLE:
        da.error = "azure-identity no esta instalado."
        return da

    code_ready = threading.Event()

    def _prompt(verification_uri, user_code, expires_on):  # noqa: ANN001
        da.verification_uri = verification_uri
        da.user_code = user_code
        da.message = (f"Ve a {verification_uri} e ingresa el codigo {user_code}")
        code_ready.set()

    def _worker():
        try:
            # disable_automatic_authentication=True evita que un get_token
            # silencioso posterior (p. ej. a Graph para otro tenant) reabra el
            # device code y bloquee. El authenticate() de abajo es EXPLICITO y
            # no se ve afectado por este flag: muestra el codigo normalmente.
            cred = _build_credential(prompt_callback=_prompt,
                                     disable_automatic_authentication=True,
                                     tenant_id=tenant_id)
            # authenticate contra el tenant indicado (satisface su MFA propio).
            auth_kwargs = {"scopes": [_ARM_SCOPE]}
            if tenant_id:
                auth_kwargs["tenant_id"] = tenant_id
            record = cred.authenticate(**auth_kwargs)
            da._result = {"credential": cred, "record": record}
        except Exception as exc:  # noqa: BLE001
            da._result = {"error": f"{type(exc).__name__}: {exc}"}
            code_ready.set()  # desbloquear aunque falle antes de emitir codigo

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    da._thread = t
    # Esperar a que el callback entregue el codigo (o falle) — pocos segundos.
    code_ready.wait(timeout=20)
    if not da.user_code and da._result.get("error"):
        da.error = da._result["error"]
    elif not da.user_code:
        da.error = "No se pudo obtener el codigo de dispositivo a tiempo."
    return da


def finish_login(da: AzureDeviceAuth, wait: int = 2) -> AzureConnection:
    """Paso 2: comprueba si el usuario ya aprobo el codigo y, de ser asi, lee
    los tenants. Si aun no, devuelve un AzureConnection con error 'pendiente'."""
    conn = AzureConnection()
    if not _AZURE_AVAILABLE:
        conn.error = "azure-identity no esta instalado."
        return conn

    t = getattr(da, "_thread", None)
    if t is not None and t.is_alive():
        t.join(timeout=wait)

    if da._result.get("error"):
        conn.error = da._result["error"]
        return conn
    if not da._result.get("credential"):
        conn.error = "pending"   # aun no aprobado
        return conn

    try:
        cred = da._result["credential"]
        record = da._result.get("record")
        conn.credential = cred
        conn.record = record
        conn.account_hint = getattr(record, "username", "") or ""
        conn.tenants = _list_tenants(cred)
        if not conn.tenants:
            conn.error = ("Autenticado, pero no se encontraron tenants accesibles "
                          "para este usuario.")
    except ClientAuthenticationError as exc:
        conn.error = f"Autenticacion fallida: {exc}"
    except Exception as exc:  # noqa: BLE001
        conn.error = f"{type(exc).__name__}: {exc}"
    return conn


def restore_connection(record_serialized: str) -> AzureConnection:
    """Reanuda la sesion de Azure desde un AuthenticationRecord serializado.

    Reconstruye la DeviceCodeCredential apoyandose en la cache de token
    persistente, SIN reabrir el navegador. Verifica que el token siga vigente
    emitiendo uno silenciosamente y re-lee los tenants. Nunca lanza.
    """
    conn = AzureConnection()
    if not _AZURE_AVAILABLE:
        conn.error = "azure-identity no esta instalado."
        return conn
    try:
        from azure.identity import AuthenticationRecord
        record = AuthenticationRecord.deserialize(record_serialized)
        # disable_automatic_authentication=True: si la cache expiro, get_token
        # LANZA en vez de abrir el device code (nunca bloquea el arranque).
        cred = _build_credential(record=record,
                                 disable_automatic_authentication=True)
        # Validar que la sesion sigue viva (token desde cache, sin navegador).
        cred.get_token(_ARM_SCOPE)
        conn.credential = cred
        conn.record = record
        conn.account_hint = getattr(record, "username", "") or ""
        conn.tenants = _list_tenants(cred)
    except Exception as exc:  # noqa: BLE001
        # Incluye AuthenticationRequiredError (cache vencida): se trata como
        # 'no hay sesion valida' y app.py pedira login limpio, sin colgarse.
        conn.error = f"{type(exc).__name__}: {exc}"
    return conn


_GRAPH_SCOPE = "https://graph.microsoft.com/.default"


def _tenant_name_from_graph(credential, tenant_id: str) -> tuple:
    """Obtiene (display_name, default_domain) del tenant via Microsoft Graph.

    El endpoint ARM (tenants.list) de azure-mgmt-subscription 3.x SOLO expone
    el GUID (no el nombre). El nombre real de la organizacion vive en Graph:
    GET /v1.0/organization  ->  value[0].displayName / verifiedDomains.

    Se emite un token de Graph PARA ESE tenant (la credencial admite todos los
    tenants por additionally_allowed_tenants=['*']). Nunca lanza: ante cualquier
    fallo devuelve ('', '').
    """
    try:
        import json
        import urllib.request

        # Token silencioso para Graph en ese tenant. Si requiere interaccion
        # (AuthenticationRequiredError, por disable_automatic_authentication),
        # el except de abajo lo captura y se cae al GUID — NUNCA bloquea.
        token = credential.get_token(_GRAPH_SCOPE, tenant_id=tenant_id).token
        req = urllib.request.Request(
            "https://graph.microsoft.com/v1.0/organization"
            "?$select=displayName,verifiedDomains",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        orgs = payload.get("value", [])
        if not orgs:
            return "", ""
        org = orgs[0]
        name = (org.get("displayName") or "").strip()
        domain = ""
        for d in org.get("verifiedDomains", []):
            if d.get("isDefault"):
                domain = (d.get("name") or "").strip()
                break
        return name, domain
    except Exception:  # noqa: BLE001
        return "", ""


def list_subscriptions(credential, tenant_id: Optional[str] = None) -> list:
    """Lista las suscripciones accesibles (opcionalmente filtradas por tenant).

    Devuelve [{'subscriptionId','displayName','state','tenantId'}]. Usa el token
    ya disponible; con disable_automatic_authentication NO reabre navegador: si
    requiere interaccion, devuelve lista vacia. Nunca lanza.
    """
    if not _AZURE_AVAILABLE:
        return []
    try:
        client = SubscriptionClient(credential)
        out = []
        for s in client.subscriptions.list():
            tid = getattr(s, "tenant_id", "") or ""
            if tenant_id and tid and tid != tenant_id:
                continue
            out.append({
                "subscriptionId": getattr(s, "subscription_id", "") or "",
                "displayName": getattr(s, "display_name", "") or "",
                "state": str(getattr(s, "state", "") or ""),
                "tenantId": tid,
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def _list_tenants(credential) -> List[Tenant]:
    """Lista los tenants visibles para el usuario autenticado, con su nombre.

    El endpoint ARM solo entrega el GUID, asi que el nombre legible se resuelve
    contra Microsoft Graph por tenant. Si Graph no responde para alguno, se deja
    el GUID como etiqueta. Usa tokens ya disponibles; no reabre el navegador.
    """
    client = SubscriptionClient(credential)
    tenants: List[Tenant] = []
    for t in client.tenants.list():
        tid = getattr(t, "tenant_id", "") or getattr(t, "id", "") or ""
        if not tid:
            continue
        name, domain = _tenant_name_from_graph(credential, tid)
        tenants.append(Tenant(id=tid, display_name=name, default_domain=domain))
    return tenants


# ---------------------------------------------------------------------------
# Prueba manual
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import time
    print("Iniciando device code login de Azure…")
    da = begin_device_login()
    if not da.ok:
        print("ERROR:", da.error)
        raise SystemExit(1)
    print(da.message)
    # Esperar aprobacion.
    for _ in range(60):
        c = finish_login(da, wait=3)
        if c.error == "pending":
            continue
        break
    if not c.ok:
        print("ERROR:", c.error)
    else:
        print(f"Usuario: {c.account_hint or '(desconocido)'}")
        print(f"Tenants accesibles: {len(c.tenants)}")
        for t in c.tenants:
            print(f"  - {t.label}")
