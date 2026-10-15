# 🛰️ Manual de funcionamiento — Kyndryl Orbit

Explica **cómo funciona Orbit por dentro**: el flujo de uso, las 7 áreas, la
caché diaria, el refresco automático y la arquitectura de despliegue. Pensado
para operadores y para quien administra la plataforma.

---

## 1. Qué es Orbit

Kyndryl Orbit es un **panel de operación multicloud** hecho en **Python +
Streamlit**. Centraliza, desde una sola pantalla, la información operativa de
las nubes de cada cliente: costos, seguridad, inventario, alertas, backups,
recursos deprecados y estado de salud. Hoy consulta **datos reales de Azure**;
la estructura está lista para sumar AWS, GCP, OCI e IBM Cloud.

El objetivo operativo: que un equipo de ~50 operadores (L1 / L2 / L3) tenga la
foto del día **ya cargada** al entrar en la mañana, sin esperar consultas lentas
a Azure.

---

## 2. Dos niveles de acceso

Orbit separa dos cosas que suelen confundirse:

1. **Login de la aplicación** — *quién* usa Orbit. Usuario y contraseña contra
   la base de datos (bcrypt). Se administra con `manage_users.py`
   (ver `manual-usuarios.md`).
2. **Conexión a la nube** — *a qué* Azure se consulta. Dentro del panel se
   conecta a Azure y se eligen el tenant (cliente) y las suscripciones.

Puedes estar logueado en Orbit sin tener aún ninguna nube conectada.

---

## 3. Flujo de uso típico

1. **Inicia sesión** en Orbit (usuario/contraseña).
2. En la **barra lateral**, sección *Nubes*, conecta **Azure**. Métodos:
   - *Interactivo (navegador)* — login con tu cuenta (soporta MFA por tenant).
   - *az login* — reutiliza la sesión de la CLI de Azure.
   - *Service Principal* — tenant + client + secret (desatendido).
3. Elige el **alcance**: tenant (cliente) y suscripciones en *Tenants · cuentas
   · alcance*.
4. Navega las **áreas** del menú central (1.1 a 1.7 + Créditos).
5. Si necesitas datos frescos al instante, usa **🔄 Refrescar datos** dentro del
   área.

---

## 4. Las 7 áreas

| # | Área | Qué muestra | Fuente de datos (Azure) |
|---|------|-------------|--------------------------|
| 1.1 | 🩺 Estado de salud | Estado operativo de cada nube | Páginas oficiales de estado |
| 1.2 | 💲 Costos | Gasto, variación, recomendaciones de ahorro | Cost Management + Consumption |
| 1.3 | 🛡️ Seguridad | Secure Score, hallazgos, recomendaciones | Defender for Cloud + Advisor |
| 1.4 | 🗂️ Inventario | Todos los recursos del alcance | Resource Graph |
| 1.5 | 🔔 Alertamiento | Alertas activas y por qué se dispararon | Azure Monitor Alerts |
| 1.6 | ⏳ Recursos deprecados | Servicios en retiro / por actualizar | Anuncios de Microsoft / Advisor |
| 1.7 | 💾 Backups | Protección, cumplimiento RPO, jobs | Recovery Services |

Cada tabla permite **exportar a CSV** (UTF-8 con BOM, compatible con Excel).

---

## 5. Caché diaria: por qué el panel carga rápido

El punto clave del diseño. Las áreas pesadas no consultan Azure en cada visita;
leen un **snapshot diario** guardado en la base de datos.

### Cómo funciona

- La función `data_cache.get_or_fetch(area, scope_key, …)` decide:
  - Si hay un snapshot del día para ese *scope* → lo devuelve
    (`source = cache`) y muestra *"📅 Snapshot diario · guardado: …"*.
  - Si no hay → consulta Azure, **guarda** el resultado y lo devuelve
    (`source = fresh`).
- El *scope* (`scope_key`) es **tenant + suscripciones**. El snapshot es
  **compartido por scope**, no por usuario.

### Qué significa en la práctica

> El operador A entra a las 8:00 y abre *Costos* del Cliente X → se consulta
> Azure y se guarda. El operador B entra a las 10:00, mismo cliente → **lee el
> snapshot** que dejó A, sin volver a consultar Azure.

### Áreas con caché diaria

`costos`, `seguridad`, `inventario`, `deprecados`, `backups`.

### La excepción: Alertamiento

**1.5 Alertamiento NO usa caché diaria.** Siempre consulta Azure Monitor en vivo
porque es la información que más necesita ser de tiempo real.

### Forzar datos frescos

El botón **🔄 Refrescar datos** de cada área ignora la caché, consulta Azure y
**actualiza** el snapshot del día.

---

## 6. Refresco diario automático (precarga)

Para que el primer acceso de la mañana **tampoco** espere, un trabajo
desatendido precarga los snapshots antes de que llegue el equipo.

- **Script:** `daily_refresh.py` recorre los clientes configurados y precarga
  las 5 áreas con caché (todas menos alertamiento).
- **Planificador:** `scheduler.py` lo dispara a las **11:00 UTC = 06:00
  America/Bogota (Colombia)**. En Docker corre como el servicio `scheduler`.
- **Autenticación:** **Service Principals por cliente** (no device-code), para
  poder correr sin intervención humana.
- **Configuración:** la lista de clientes va en `ORBIT_DAILY_CLIENTS` (JSON),
  idealmente desde AWS Secrets Manager.

### Estado actual: DESACTIVADO

`ORBIT_DAILY_CLIENTS` está **vacío** a propósito, hasta tener los Service
Principals de cada cliente. Mientras tanto el job corre, no encuentra clientes y
termina sin hacer nada (es seguro). Cuando se agreguen los SP, la precarga
empieza automáticamente.

Prueba manual:

```bash
python daily_refresh.py --dry-run   # muestra qué haría
python daily_refresh.py             # ejecuta (no-op si la lista está vacía)
```

---

## 7. Dónde se guardan los datos

Todo en la base de datos (PostgreSQL en producción, SQLite en local):

| Tabla | Contenido |
|-------|-----------|
| `users` | Usuarios de la app (hash bcrypt, rol, último login). |
| `app_sessions` | Sesión de nube persistida por usuario. |
| `cloud_connections` | Metadatos de conexiones por usuario/nube. |
| `history_snapshots` | **Snapshots diarios** por área y scope (la caché diaria). |

El dato pesado de cada snapshot se serializa dentro de `history_snapshots`
(junto con sus KPIs), de modo que al leerlo se reconstruye tal cual (por
ejemplo, las tablas de inventario o costos).

Los **secretos** (credenciales de Azure, tokens) **no** van en estas tablas: se
leen de variables de entorno / `.env` o de **AWS Secrets Manager**, según
`SECRETS_BACKEND`.

---

## 8. Componentes del sistema

```
Navegador  ──HTTPS──►  app (Streamlit)
                          │
                          ├── PostgreSQL  (usuarios, sesiones, snapshots)
                          ├── Redis       (caché efímera / sesiones compartidas)
                          └── Azure APIs  (costos, seguridad, inventario, …)

scheduler (06:00 Colombia) ──► daily_refresh ──► Azure ──► PostgreSQL (snapshots)
```

- **app** — la interfaz; sirve las áreas leyendo de la caché o de Azure.
- **db** (PostgreSQL) — persistencia; sobrevive reinicios del contenedor.
- **redis** — caché efímera y de corta vida; se puede reiniciar sin pérdida.
- **scheduler** — precarga diaria; inofensivo hasta configurar clientes.

---

## 9. Arquitectura de despliegue (AWS)

Resumen del diagrama `arquitectura.png`:

- **Cómputo:** una instancia **EC2** (Linux, Amazon Linux 2023) ejecutando los
  contenedores con **Docker Compose**.
- **Dentro de la EC2 (Docker):** los contenedores `app`, `db` (PostgreSQL),
  `redis` y `scheduler`. PostgreSQL persiste en un **volumen Docker** respaldado
  por **EBS**.
- **Imágenes:** se publican en **Amazon ECR**.
- **CI/CD:** **GitHub Actions** (build, test, lint/security scan, push a ECR,
  deploy a EC2).
- **Secretos y configuración:** **Secrets Manager** y **Parameter Store**; la
  EC2 los lee con un **IAM Role** de mínimo privilegio (sin claves en disco).
- **Observabilidad:** **CloudWatch** (logs y métricas) y **CloudTrail**
  (auditoría).
- **Acceso:** HTTPS. Opcionalmente **WAF + ALB** si se requiere mayor exposición.
- **Costo estimado:** ~54 USD/mes para uso moderado con 50 usuarios (t3.medium);
  escalable a instancias mayores o Auto Scaling si crece la carga.

> Nota importante: PostgreSQL y Redis corren **como contenedores dentro de la
> EC2** (vía compose), no como servicios gestionados. La persistencia de la DB
> está en el volumen EBS. Si en el futuro se quiere alta disponibilidad,
> conviene migrar PostgreSQL a **RDS** y Redis a **ElastiCache**.

---

## 10. Problemas frecuentes

| Síntoma | Causa | Solución |
|---------|-------|----------|
| El panel muestra datos "viejos" | Vienen del snapshot diario | Usa **🔄 Refrescar datos** |
| Azure conecta pero sin suscripciones | Login en el tenant equivocado | Ingresa el Tenant ID correcto |
| Alertas no coinciden con el panel | Alertamiento es tiempo real; el resto es snapshot | Es el comportamiento esperado |
| El refresco diario no carga nada | `ORBIT_DAILY_CLIENTS` vacío | Normal hasta tener los Service Principals |
| Error de `libsecret` en contenedor | Linux sin llavero | Ya resuelto (caché de token sin cifrar en contenedor) |
