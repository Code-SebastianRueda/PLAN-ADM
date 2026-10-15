# 👤 Manual de administración de usuarios — Kyndryl Orbit

Este manual explica cómo **crear, listar y administrar** los usuarios que
pueden iniciar sesión en Kyndryl Orbit.

> Aplica al **login de la aplicación** (quién entra a Orbit). Es distinto de la
> **conexión a la nube** (a qué Azure se consulta), que se hace dentro del panel
> y no se administra aquí.

---

## 1. Cómo funciona la autenticación

- Los usuarios se guardan en la base de datos (tabla `users`).
- La contraseña **nunca** se guarda en claro: se almacena con hash **bcrypt**.
- Cada usuario tiene un **rol**: `admin` o `user`.
- En el **primer arranque**, si no hay usuarios, Orbit crea un admin inicial:
  - Usuario: `admin`
  - Contraseña: `planadm2026`
  - 👉 **Cámbiala de inmediato** (ver §4).
  - Puedes cambiar ese seed con las variables `PLANADM_USER` /
    `PLANADM_PASSWORD` antes del primer arranque.

Hoy la gestión de usuarios se hace con el script `manage_users.py` (no hay panel
gráfico todavía). El script escribe en la **misma base de datos** que usa la
app: PostgreSQL si definiste `DATABASE_URL`, o el SQLite local `planadm.db` si
no.

---

## 2. Dónde ejecutar los comandos

### Local (sin Docker)

```powershell
cd C:\Users\jsgru\OneDrive\Escritorio\Repos\PLAN-ADM
python manage_users.py <comando>
```

### Con Docker (producción)

Ejecuta dentro del contenedor `app`, para que use la misma PostgreSQL:

```bash
docker compose exec app python manage_users.py <comando>
```

> Si lo corres en local pero quieres apuntar a la PostgreSQL de producción,
> exporta `DATABASE_URL` antes del comando.

---

## 3. Crear un usuario

```bash
python manage_users.py add jperez --role user --name "Juan Pérez"
```

- `jperez` → nombre de usuario (con el que inicia sesión).
- `--role` → `user` (por defecto) o `admin`.
- `--name` → nombre para mostrar (opcional).

El script **pide la contraseña de forma interactiva** (no se escribe en el
comando, así no queda en el historial de la terminal). Debe tener al menos
**8 caracteres** y confirmarse dos veces.

```
Nueva contraseña: ********
Repite la contraseña: ********
Usuario creado.
```

---

## 4. Cambiar una contraseña

```bash
python manage_users.py passwd admin
```

Pide la nueva contraseña dos veces. Úsalo para cambiar la contraseña por defecto
del admin tras el primer ingreso.

---

## 5. Listar usuarios

```bash
python manage_users.py list
```

```
USUARIO              ROL      ACTIVO  ÚLTIMO LOGIN         NOMBRE
--------------------------------------------------------------------------------
admin                admin    sí      2026-10-15T00:39:26  Administrador
jperez               user     sí      -                    Juan Pérez
```

---

## 6. Cambiar el rol de un usuario

```bash
python manage_users.py role jperez admin     # ascender a admin
python manage_users.py role jperez user      # volver a usuario normal
```

---

## 7. Desactivar / reactivar un usuario

Desactivar bloquea el login **sin borrar** la cuenta (útil cuando alguien sale
temporalmente del equipo):

```bash
python manage_users.py disable jperez
python manage_users.py enable  jperez
```

---

## 8. Eliminar un usuario

Elimina la cuenta **definitivamente**. Pide confirmación escribiendo `SI`:

```bash
python manage_users.py delete jperez
```

---

## 9. Resumen de comandos

| Comando | Qué hace |
|---------|----------|
| `list` | Lista todos los usuarios. |
| `add <usuario> [--role] [--name]` | Crea un usuario (pide contraseña). |
| `passwd <usuario>` | Cambia la contraseña. |
| `role <usuario> <admin\|user>` | Cambia el rol. |
| `disable <usuario>` | Bloquea el login (sin borrar). |
| `enable <usuario>` | Reactiva. |
| `delete <usuario>` | Elimina definitivamente. |

---

## 10. Buenas prácticas

- Cambia la contraseña del admin por defecto **en el primer ingreso**.
- Da rol `admin` solo a quien administra usuarios o configuración.
- Para ~50 operadores: crea una cuenta por persona (no compartan credenciales),
  así el campo *último login* sirve de bitácora.
- En producción, define `PLANADM_PASSWORD` en `.env` para que el admin inicial
  no sea el valor por defecto.
- Las credenciales de Azure (Service Principals, secretos) **no** se guardan
  aquí: van en `.env` o AWS Secrets Manager (ver el manual de funcionamiento).
