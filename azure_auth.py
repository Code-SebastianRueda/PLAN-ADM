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

    @property
    def label(self) -> str:
        name = self.display_name or self.id
        return f"{name} · {self.id}"


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


def _build_credential(prompt_callback=None, record=None):
    """Crea una DeviceCodeCredential (flujo de codigo de dispositivo).

    Device code NO usa servidor de redirect local, por lo que elimina de raiz
    el 'state mismatch' que sufre el flujo de navegador dentro de Streamlit.

    - prompt_callback(verification_uri, user_code, expires_on) -> para mostrar
      el codigo al usuario.
    - additionally_allowed_tenants=["*"] -> la MISMA credencial podra emitir
      tokens para el tenant que se elija luego en el panel, sin reautenticar.
    - authentication_record (si existe)  -> reanuda sesion desde la cache.
    """
    kwargs = dict(
        cache_persistence_options=_cache_opts(),
        additionally_allowed_tenants=["*"],
    )
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


def begin_device_login() -> AzureDeviceAuth:
    """Paso 1: inicia el device code en un hilo y devuelve el codigo a mostrar.

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
            cred = _build_credential(prompt_callback=_prompt)
            record = cred.authenticate(scopes=[_ARM_SCOPE])
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


def _list_tenants(credential) -> List[Tenant]:
    """Lista los tenants visibles para el usuario autenticado.

    Usa el token ya emitido (home tenant); no requiere login adicional.
    """
    client = SubscriptionClient(credential)
    tenants: List[Tenant] = []
    for t in client.tenants.list():
        tenants.append(Tenant(
            id=getattr(t, "tenant_id", "") or "",
            display_name=(getattr(t, "display_name", None)
                          or getattr(t, "default_domain", None) or ""),
        ))
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
