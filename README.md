# ☁️ Plan de Administración Operativa Cloud (PDM)

Aplicación web en **Python + Streamlit** para gestionar integralmente la
operación multicloud (**AWS** + **Azure**) según la matriz operativa de los
tres niveles (L1, L2, L3): tareas proactivas, mantenimientos, remediación,
gobierno y **KPIs** con indicadores de color.

Las consultas se ejecutan **en segundo plano** (paralelizadas y cacheadas) para
que la navegación sea fluida incluso con muchas suscripciones.

## 📁 Estructura

```
PLAN-ADM/
├── app.py             # Orquestador Streamlit (login, RBAC, tabs, KPIs, export)
├── aws_services.py    # AWS (boto3): extracción + acciones L1/L2/L3
├── azure_services.py  # Azure (SDK): descubrimiento + L1/L2/L3 (multi-sub paralelo)
├── kpi_engine.py      # Cálculo de KPIs por nivel (fórmulas + estados de color)
├── requirements.txt
└── README.md
```

## 🧭 Módulos por nivel (matriz operativa)

**L1 — Operación y Monitoreo Proactivo** (pestañas Diario/Semanal/Mensual + KPIs)
- 1.1 Salud de infraestructura · 1.3 Seguridad (Security Hub/Defender) · Alertas (CloudWatch/Monitor)
- 1.6 Recursos huérfanos · 1.7 Respaldo (AWS Backup / Recovery Services) · 1.5 Servicios deprecados
- 1.4 Costos (7 días + mes)
- KPIs: **MTTA (≤15 min)**, **MTTR (≤2h)**, **% Falsos Positivos (≤10%)**, **% Proactividad (≥80%)**

**L2 — Soporte Avanzado y Gestión Operativa** (Validación/Parches/Mantenimiento + KPIs)
- 1.1 Bandeja de hallazgos escalados (tomar acción / descartar)
- 1.2 Upgrades y parches (SSM Patch Manager / Advisor)
- 1.5 Ventanas de mantenimiento (planificador SSM)
- KPIs: **% Escalamientos sin error (≥98%)**, **% Cumplimiento de cambios (≥95%)**, **% Reducción de costos (≥10%)**

**L3 — Arquitectura, Automatización y Mejora Continua** (Arquitectura/Optimización + KPIs)
- 1.1 Well-Architected · 1.2 Playbooks de automatización (SSM / Automation Accounts)
- 1.4 Right-sizing + proyección financiera (Savings Plans / Reservas) · 1.5 Cuotas
- KPIs: **% Automatizaciones (≥90%)**, **% Proyectos sin incidentes (≥95%)**, **% Mejora de rendimiento (≥15%)**

Cada tabla tiene botones de exportación **CSV y Excel**.

---

## 1️⃣ Requisitos previos

- **Python 3.9+**
- **AWS CLI v2** con IAM Identity Center (SSO) configurado
- **Azure CLI** (opcional si usas login interactivo en la app)

---

## 2️⃣ Instalación (Python global)

```powershell
cd C:\Users\jsgru\OneDrive\Escritorio\Repos\PLAN-ADM
python -m pip install -r requirements.txt
```

---

## 3️⃣ Inicio de sesión local

### 🟠 AWS (SSO)

```bash
aws configure sso          # una sola vez
aws sso login --profile <perfil>
```

La app lista automáticamente los perfiles de `~/.aws/config` y puede lanzar el
login SSO desde la interfaz.

### 🔵 Azure

- **Interactivo:** elige *Interactivo (navegador)*, ingresa el **Tenant ID** que
  contiene tus suscripciones y conecta. Se descubren todas automáticamente.
- **az login:** `az login` en terminal + opción *az login (Default)*.
- **Service Principal:** Tenant/Client/Secret.

> 💡 Si al conectar no aparecen suscripciones, casi siempre es porque el login
> cayó en un **tenant distinto** al de tus suscripciones. La app te muestra los
> tenants disponibles: copia el Tenant ID correcto y reintenta.

---

## 4️⃣ Ejecutar

```bash
python -m streamlit run app.py
```

Abre `http://localhost:8501`.

**Flujo:** Fase 1 (login/descubrimiento) → *Entrar al panel* → Fase 2 (elige
Nube + Alcance de suscripciones + Rol L1/L2/L3). Usa **🔄 Refrescar datos** para
invalidar la caché y volver a consultar.

---

## 🔐 Permisos IAM / RBAC

**AWS**
- Lectura: `sts:GetCallerIdentity`, `health:DescribeEvents`, `ce:Get*`,
  `ec2:Describe*`, `elasticloadbalancing:Describe*`, `securityhub:GetFindings`,
  `securityhub:GetEnabledStandards`, `cloudwatch:DescribeAlarms`,
  `backup:ListBackupJobs`, `ssm:Describe*`, `ssm:ListDocuments`,
  `compute-optimizer:Get*`, `servicequotas:List*`
- Acciones L2: `ec2:DeleteVolume`, `ec2:ReleaseAddress`,
  `elasticloadbalancing:DeleteLoadBalancer`, `securityhub:BatchUpdateFindings`,
  `ssm:SendCommand`, `ssm:CreateMaintenanceWindow`

**Azure** (por suscripción)
- Lectura: `Reader`, `Cost Management Reader`, `Security Reader`
- Acciones L2: `Contributor` (o `Disk/Network Contributor`) y `Security Admin`

---

## ⚡ Rendimiento

- Azure multi-suscripción usa `ThreadPoolExecutor` (hasta 12 hilos) para barrer
  todas las subs en paralelo.
- Los resultados se **cachean** en `session_state`: cambiar de pestaña no vuelve
  a consultar. Usa el botón de refrescar para forzar recarga.

---

## 🧯 Solución de problemas

| Síntoma | Causa | Solución |
|---------|-------|----------|
| `ModuleNotFoundError` | Se ejecutó con `python app.py` | Usa `python -m streamlit run app.py` |
| Azure sin suscripciones | Login en tenant equivocado | Ingresa el Tenant ID correcto |
| "Sesión expirada" (AWS) | Token SSO vencido | `aws sso login --profile <perfil>` |
| Tabla con nota de plan/permiso | Servicio no habilitado o sin rol | Habilitar servicio / añadir permisos |

---

## ⚠️ Notas

- Las acciones **L2 son destructivas**; la app exige confirmación explícita.
- Los KPIs se registran manualmente (bitácora operativa) y se evalúan contra las
  metas de la matriz, con indicadores 🟢/🟡/🔴.
- Exportación CSV (UTF-8 con BOM) y Excel (openpyxl) en todas las tablas.
