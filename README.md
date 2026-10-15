# 🛰️ Kyndryl Orbit — Multi-Cloud Automation & Governance

Aplicación web en **Python + Streamlit** para la **operación multicloud** desde
un solo panel. Hoy consulta **datos reales de Azure** (costos, seguridad,
inventario, alertas, backups, recursos deprecados y estado de salud) y está
preparada para sumar AWS, GCP, OCI e IBM Cloud.

Pensada para un equipo de operadores cloud (L1 / L2 / L3). Los datos pesados se
**precargan cada mañana** y se sirven desde una **caché diaria en base de
datos**, de modo que al entrar el panel ya está listo sin esperar a Azure. El
**alertamiento es la única área en tiempo real**.

> Nombre técnico del repositorio y de los contenedores: `plan-adm`
> (histórico). El producto se llama **Kyndryl Orbit**.

---

## 📚 Documentación

| Documento | Para qué sirve |
|-----------|----------------|
| **README.md** (este archivo) | Visión general, instalación y ejecución. |
| [`docs/manual-funcionamiento.md`](docs/manual-funcionamiento.md) | Cómo funciona la app por dentro: áreas, caché diaria, autenticación. |
| [`docs/manual-usuarios.md`](docs/manual-usuarios.md) | Cómo crear, listar y administrar usuarios. |
| `arquitectura.png` | Diagrama de la arquitectura de despliegue en AWS (EC2 + Docker). |

---

## 🧭 Áreas del panel

El menú central tiene 7 áreas funcionales más Créditos:

| # | Área | Fuente real (Azure) | Caché diaria |
|---|------|---------------------|:---:|
| 1.1 | 🩺 **Estado de salud** | Páginas oficiales de estado de las nubes | — (ligero) |
| 1.2 | 💲 **Costos** | Azure Cost Management + Consumption (budgets) | ✅ |
| 1.3 | 🛡️ **Seguridad** | Microsoft Defender for Cloud + Advisor | ✅ |
| 1.4 | 🗂️ **Inventario** | Azure Resource Graph | ✅ |
| 1.5 | 🔔 **Alertamiento** | Azure Monitor Alerts | ❌ **tiempo real** |
| 1.6 | ⏳ **Recursos deprecados** | Anuncios de retiro de Microsoft / Advisor | ✅ |
| 1.7 | 💾 **Backups** | Recovery Services (vaults, items, jobs) | ✅ |
| ℹ️ | **Créditos** | Equipo y versión | — |

Las áreas con caché diaria muestran el aviso *"📅 Snapshot diario · guardado:
…"* cuando los datos vienen de la caché.

---

## 📁 Estructura del proyecto

```
PLAN-ADM/
├── app.py                 # UI Streamlit: login de app, sidebar, 7 áreas, Créditos
│
│  ── Autenticación y sesión ──
├── app_auth.py            # Login de la app (bcrypt en DB + fallback env + admin inicial)
├── azure_auth.py          # Conexión a Azure (interactivo/device-code, az login, SP) + subs
├── aws_auth.py            # Conexión a AWS (perfiles / SSO) — base para integración
├── session_store.py       # Persistencia de la sesión de nube (DB)
│
│  ── Datos reales de Azure (una por área) ──
├── azure_costs.py         # 1.2 Costos
├── azure_security.py      # 1.3 Seguridad
├── azure_inventory.py     # 1.4 Inventario
├── azure_alerts.py        # 1.5 Alertamiento (tiempo real)
├── azure_deprecations.py  # 1.6 Recursos deprecados
├── azure_backups.py       # 1.7 Backups
├── cloud_status.py        # 1.1 Estado de salud (estados oficiales)
│
│  ── Infraestructura / plataforma ──
├── db.py                  # Capa de datos (SQLAlchemy): usuarios, sesiones, snapshots
├── data_cache.py          # Caché diaria: get_or_fetch (fresh vs cache por scope)
├── cache_store.py         # Caché efímera (Redis si está disponible)
├── secrets_provider.py    # Secretos: entorno/.env o AWS Secrets Manager
│
│  ── Refresco diario desatendido ──
├── daily_refresh.py       # Precarga los snapshots por cliente (Service Principals)
├── scheduler.py           # Dispara daily_refresh a las 11:00 UTC (06:00 Colombia)
│
│  ── Despliegue ──
├── Dockerfile             # Imagen de la app (Streamlit)
├── docker-compose.yml     # app + PostgreSQL + Redis + scheduler
├── .env.example           # Plantilla de variables de entorno
├── requirements.txt
├── arquitectura.png
└── docs/                  # Manuales (funcionamiento y usuarios)
```

---

## 1️⃣ Requisitos previos

- **Python 3.11** (local). En contenedor ya viene fijado en el `Dockerfile`.
- **Docker + Docker Compose** (para el despliegue con PostgreSQL + Redis).
- Acceso a Azure con permisos de lectura (ver [Permisos](#-permisos-necesarios-en-azure)).

---

## 2️⃣ Ejecución local (desarrollo rápido)

Sin Docker, usando SQLite como base de datos de respaldo:

```powershell
cd C:\Users\jsgru\OneDrive\Escritorio\Repos\PLAN-ADM
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.port 8501
```

Abre `http://localhost:8501`.

- **Base de datos:** si no defines `DATABASE_URL`, usa el archivo local
  `planadm.db` (SQLite). No necesitas instalar PostgreSQL para probar.
- **Login de la app:** usuario `admin` / contraseña `planadm2026`
  (**cámbialo** tras el primer ingreso — ver `docs/manual-usuarios.md`).

---

## 3️⃣ Ejecución con Docker (recomendado)

Levanta el stack completo: app + PostgreSQL + Redis + el planificador diario.

```bash
cp .env.example .env        # ajusta contraseñas y variables
docker compose up -d --build
```

Abre `http://<host>:8501`.

Servicios que levanta:

| Servicio | Imagen | Rol |
|----------|--------|-----|
| `app` | `plan-adm:latest` | La UI Streamlit (puerto 8501). |
| `db` | `postgres:16-alpine` | Base de datos (usuarios, sesiones, snapshots). |
| `redis` | `redis:7-alpine` | Caché efímera / sesiones compartidas. |
| `scheduler` | `plan-adm:latest` | Refresco diario (11:00 UTC = 06:00 Colombia). |

> El servicio `scheduler` corre siempre, pero **no hace nada** hasta que se
> configuren los clientes en `ORBIT_DAILY_CLIENTS` (ver abajo). Es seguro.

---

## 4️⃣ Variables de entorno

Todas en `.env` (plantilla en `.env.example`). Las principales:

| Variable | Para qué | Por defecto |
|----------|----------|-------------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Credenciales de PostgreSQL (compose arma `DATABASE_URL`). | `planadm` |
| `DATABASE_URL` | DB externa (p. ej. RDS). Si está, tiene prioridad. | *(SQLite local)* |
| `PLANADM_USER` / `PLANADM_PASSWORD` | Admin inicial de la app (seed). | `admin` / `planadm2026` |
| `SECRETS_BACKEND` | `env` (lee de .env) o `aws` (Secrets Manager). | `env` |
| `REDIS_URL` | Caché efímera. | `redis://redis:6379/0` |
| `ORBIT_REFRESH_UTC_HOUR` / `_MINUTE` | Hora del refresco diario (UTC). | `11` / `0` |
| `ORBIT_DAILY_CLIENTS` | JSON con los Service Principals a precargar. **Vacío = refresco desactivado.** | *(vacío)* |

---

## 🔄 Refresco diario (caché por cliente)

Cada mañana el servicio `scheduler` ejecuta `daily_refresh.py`, que precarga los
snapshots de **costos, seguridad, inventario, deprecados y backups** por cada
cliente y los guarda en la DB (`history_snapshots`), compartidos por *scope*
(tenant + suscripciones). Así, cuando el equipo entra a las ~8 AM, el primer
acceso ya encuentra todo cacheado.

- **Hora:** 11:00 UTC = **06:00 America/Bogota**.
- **Autenticación:** **Service Principals por cliente** (no device-code), para
  que corra desatendido.
- **Estado:** **DESACTIVADO** hasta tener los accesos. La lista
  `ORBIT_DAILY_CLIENTS` está vacía; el job corre y termina sin hacer nada.

Formato de `ORBIT_DAILY_CLIENTS` (una sola línea):

```json
[{"name":"Cliente A","tenant_id":"...","client_id":"...","client_secret":"..."}]
```

Prueba manual (seguro aunque esté vacío):

```bash
python daily_refresh.py --dry-run   # lista lo que haría
python daily_refresh.py             # ejecuta (no-op si no hay clientes)
```

> **Alertamiento NO entra en el refresco diario**: siempre consulta Azure
> Monitor en vivo.

---

## 🔐 Autenticación

Dos niveles independientes:

1. **Login de la aplicación** (quién usa Orbit): usuario + contraseña,
   verificado contra la DB con **bcrypt**. Multiusuario con rol `admin` o
   `user`. Ver `docs/manual-usuarios.md`.
2. **Conexión a la nube** (a qué Azure se consulta): dentro del panel se conecta
   a Azure por *Interactivo (navegador)*, *az login* o *Service Principal*, y se
   eligen el tenant y las suscripciones (alcance).

---

## 🔑 Permisos necesarios en Azure

Por suscripción, roles de **solo lectura**:

- `Reader`
- `Cost Management Reader` (área de Costos)
- `Security Reader` (área de Seguridad / Defender)

Para el refresco diario desatendido, el **Service Principal de cada cliente**
necesita esos mismos roles sobre las suscripciones del cliente.

---

## 🧯 Solución de problemas

| Síntoma | Causa | Solución |
|---------|-------|----------|
| `ModuleNotFoundError` al arrancar | Se ejecutó con `python app.py` | Usa `python -m streamlit run app.py` |
| Azure sin suscripciones | Login en el tenant equivocado | Ingresa el Tenant ID correcto; la app lista los tenants disponibles |
| Error de `libsecret` en contenedor | Linux sin llavero gráfico | Ya resuelto: la caché de token va sin cifrar (`AZURE_TOKEN_CACHE_UNENCRYPTED=true`) |
| Advisor / deprecados devuelve HTTP 500 | Limitación del tenant | La app reintenta y degrada con gracia |
| El panel no actualiza datos | Vienen de la caché diaria | Usa **🔄 Refrescar datos** en el área para forzar consulta |

---

## 📦 Despliegue en AWS

El diagrama `arquitectura.png` resume el despliegue objetivo: **EC2 + Docker** en
`us-east-1`, imagen publicada vía **ECR**, CI/CD con **GitHub Actions**,
secretos en **Secrets Manager / Parameter Store**, logs en **CloudWatch** y
auditoría con **CloudTrail**. PostgreSQL y Redis corren como contenedores dentro
de la misma EC2 (vía `docker-compose`), con persistencia de PostgreSQL en un
volumen Docker respaldado por EBS.
