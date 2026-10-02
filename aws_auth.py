"""
aws_auth.py
===========
Login REAL de AWS IAM Identity Center (SSO) para PLAN-ADM mediante el flujo
de autorizacion de dispositivo (OIDC Device Authorization Grant).

Flujo (lo que ve el usuario):
    1. start_device_authorization  -> la app obtiene una URL de aprobacion.
    2. El usuario abre esa URL y APRUEBA el acceso en AWS (en el navegador).
    3. create_token (polling)       -> al aprobar, la app recibe un accessToken.
    4. list_accounts / list_account_roles -> se descubren las cuentas y roles
       a los que el usuario tiene acceso en ese portal SSO.

Como Streamlit re-ejecuta el script, el flujo se parte en dos pasos:
    - begin_device_authorization(): registra cliente + inicia device auth y
      devuelve los datos para mostrar el enlace de aprobacion.
    - complete_login(): tras aprobar, intercambia el device code por el token
      y lista cuentas/roles.

No depende de Streamlit: solo logica AWS, para poder probarlo aislado.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import boto3
    from botocore.exceptions import ClientError
    _AWS_AVAILABLE = True
except Exception:  # noqa: BLE001
    _AWS_AVAILABLE = False


# Region del endpoint de AWS SSO/OIDC (no es la region de los recursos).
_SSO_REGION = "us-east-1"
_CLIENT_NAME = "PLAN-ADM"
# Scope estandar del portal SSO para listar cuentas.
_SSO_SCOPES = ["sso:account:access"]


class AWSAuthError(Exception):
    """Error de autenticacion o descubrimiento en AWS SSO."""


@dataclass
class AWSRole:
    role_name: str
    account_id: str


@dataclass
class AWSAccount:
    account_id: str
    account_name: str = ""
    email: str = ""
    roles: List[AWSRole] = field(default_factory=list)

    @property
    def label(self) -> str:
        name = self.account_name or self.account_id
        return f"{name} ({self.account_id})"


@dataclass
class AWSDeviceAuth:
    """Datos intermedios del paso 1 (para mostrar el enlace de aprobacion)."""
    client_id: str = ""
    client_secret: str = ""
    device_code: str = ""
    user_code: str = ""
    verification_uri: str = ""
    verification_uri_complete: str = ""
    interval: int = 5
    expires_in: int = 600
    start_url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.device_code) and not self.error


@dataclass
class AWSConnection:
    client_name: str = ""          # cliente logico (Bolivar, Coomeva, …)
    start_url: str = ""
    access_token: str = ""
    expires_at: float = 0.0        # epoch
    sso_region: str = _SSO_REGION
    accounts: List[AWSAccount] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.access_token) and not self.error

    @property
    def total_roles(self) -> int:
        return sum(len(a.roles) for a in self.accounts)


# ---------------------------------------------------------------------------
# Normalizacion del start URL
# ---------------------------------------------------------------------------
def normalize_start_url(url: str) -> str:
    """Convierte la URL del portal (con #/...) en el start URL base del SSO."""
    if not url:
        return url
    base = url.split("#", 1)[0].rstrip("/")
    # El start URL del SSO suele terminar en /start.
    if not base.endswith("/start"):
        if base.endswith(".awsapps.com"):
            base = base + "/start"
    return base


# ---------------------------------------------------------------------------
# Paso 1: iniciar autorizacion de dispositivo
# ---------------------------------------------------------------------------
def begin_device_authorization(start_url: str,
                               sso_region: str = _SSO_REGION) -> AWSDeviceAuth:
    """Registra el cliente OIDC e inicia el device authorization.

    Devuelve AWSDeviceAuth con la URL que el usuario debe abrir para aprobar.
    Nunca lanza: los errores quedan en .error.
    """
    da = AWSDeviceAuth(start_url=normalize_start_url(start_url))
    if not _AWS_AVAILABLE:
        da.error = "boto3 no esta instalado."
        return da
    try:
        oidc = boto3.client("sso-oidc", region_name=sso_region)
        # Nota: igual que la AWS CLI, NO se envian 'scopes' en register_client;
        # el portal otorga el scope por defecto (sso:account:access). Enviar
        # scopes puede impedir la emision del token tras aprobar.
        reg = oidc.register_client(clientName=_CLIENT_NAME, clientType="public")
        da.client_id = reg["clientId"]
        da.client_secret = reg["clientSecret"]

        auth = oidc.start_device_authorization(
            clientId=da.client_id,
            clientSecret=da.client_secret,
            startUrl=da.start_url,
        )
        da.device_code = auth["deviceCode"]
        da.user_code = auth.get("userCode", "")
        da.verification_uri = auth.get("verificationUri", "")
        da.verification_uri_complete = auth.get("verificationUriComplete", "")
        da.interval = int(auth.get("interval", 5))
        da.expires_in = int(auth.get("expiresIn", 600))
    except ClientError as exc:
        da.error = _client_error_msg(exc)
    except Exception as exc:  # noqa: BLE001
        da.error = f"{type(exc).__name__}: {exc}"
    return da


# ---------------------------------------------------------------------------
# Paso 2: completar login (tras aprobar) + listar cuentas/roles
# ---------------------------------------------------------------------------
def complete_login(da: AWSDeviceAuth, client_name: str = "",
                   sso_region: str = _SSO_REGION,
                   max_wait: int = 60) -> AWSConnection:
    """Intercambia el device code por el token (esperando la aprobacion) y
    descubre cuentas/roles. Nunca lanza: errores en .error."""
    conn = AWSConnection(client_name=client_name, start_url=da.start_url)
    if not _AWS_AVAILABLE:
        conn.error = "boto3 no esta instalado."
        return conn
    try:
        oidc = boto3.client("sso-oidc", region_name=sso_region)
        token = _poll_for_token(oidc, da, max_wait=max_wait)
        if not token:
            conn.error = ("No se recibio la aprobacion a tiempo. Abre el enlace, "
                          "aprueba el acceso y vuelve a intentar.")
            return conn

        # El login SOLO obtiene el token. El listado de cuentas/roles se hace
        # despues, en el panel central (igual que Azure con sus tenants).
        conn.access_token = token["accessToken"]
        conn.expires_at = time.time() + int(token.get("expiresIn", 3600))
        conn.sso_region = sso_region
    except ClientError as exc:
        conn.error = _client_error_msg(exc)
    except Exception as exc:  # noqa: BLE001
        conn.error = f"{type(exc).__name__}: {exc}"
    return conn


def _poll_for_token(oidc, da: AWSDeviceAuth, max_wait: int) -> Optional[Dict]:
    """Intenta canjear el device code por el token.

    Como se invoca DESPUES de que el usuario pulsa 'Ya aprobe', hace un intento
    inmediato y unos pocos reintentos cortos (max_wait segundos). Si sigue
    pendiente, devuelve None para que la UI pida reintentar, en vez de bloquear
    el hilo de Streamlit durante mucho tiempo.
    """
    deadline = time.time() + max_wait
    interval = 2
    first = True
    while first or time.time() < deadline:
        first = False
        try:
            return oidc.create_token(
                clientId=da.client_id,
                clientSecret=da.client_secret,
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=da.device_code,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("AuthorizationPendingException", "SlowDownException"):
                if code == "SlowDownException":
                    interval += 2
                if time.time() + interval >= deadline:
                    break
                time.sleep(interval)
                continue
            # ExpiredTokenException, AccessDeniedException, etc. -> abortar.
            raise
    return None


# ---------------------------------------------------------------------------
# Listado bajo demanda (se usa en el PANEL, no en el login)
# ---------------------------------------------------------------------------
def list_accounts(access_token: str, sso_region: str = _SSO_REGION) -> List[AWSAccount]:
    """Lista SOLO las cuentas (rapido). Los roles se cargan aparte por cuenta.

    Nunca lanza: ante error devuelve lista vacia.
    """
    if not _AWS_AVAILABLE:
        return []
    try:
        sso = boto3.client("sso", region_name=sso_region)
        accounts: List[AWSAccount] = []
        paginator = sso.get_paginator("list_accounts")
        for page in paginator.paginate(accessToken=access_token):
            for a in page.get("accountList", []):
                accounts.append(AWSAccount(
                    account_id=a.get("accountId", ""),
                    account_name=a.get("accountName", ""),
                    email=a.get("emailAddress", ""),
                ))
        return accounts
    except Exception:  # noqa: BLE001
        return []


def list_account_roles(access_token: str, account_id: str,
                       sso_region: str = _SSO_REGION) -> List[AWSRole]:
    """Lista los roles de UNA cuenta (bajo demanda, al seleccionarla en el panel)."""
    if not _AWS_AVAILABLE:
        return []
    try:
        sso = boto3.client("sso", region_name=sso_region)
        roles: List[AWSRole] = []
        paginator = sso.get_paginator("list_account_roles")
        for page in paginator.paginate(accessToken=access_token, accountId=account_id):
            for r in page.get("roleList", []):
                roles.append(AWSRole(role_name=r.get("roleName", ""),
                                     account_id=account_id))
        return roles
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client_error_msg(exc) -> str:
    err = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    code = err.get("Code", "ClientError")
    msg = err.get("Message", str(exc))
    return f"{code}: {msg}"


# ---------------------------------------------------------------------------
# Prueba manual
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import webbrowser
    START = "https://solucionesbolivar.awsapps.com/start"
    print("Iniciando device authorization de AWS SSO…")
    da = begin_device_authorization(START)
    if not da.ok:
        print("ERROR:", da.error)
        raise SystemExit(1)
    print("Abre esta URL y aprueba el acceso:")
    print("  ", da.verification_uri_complete or da.verification_uri)
    print("  Codigo:", da.user_code)
    try:
        webbrowser.open(da.verification_uri_complete or da.verification_uri)
    except Exception:  # noqa: BLE001
        pass
    conn = complete_login(da, client_name="Bolivar", max_wait=da.expires_in)
    if not conn.ok:
        print("ERROR:", conn.error)
    else:
        print(f"Conectado (token OK). Listando cuentas…")
        accts = list_accounts(conn.access_token, conn.sso_region)
        print(f"Cuentas: {len(accts)}")
        for a in accts:
            print(f"  - {a.label}")
