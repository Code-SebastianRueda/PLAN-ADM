"""
manage_users.py
===============
Administración de usuarios de Kyndryl Orbit desde la línea de comandos.

Orbit no tiene (todavía) un panel gráfico de usuarios: la gestión se hace con
este script, que escribe en la MISMA base de datos que usa la app (la que
indique DATABASE_URL, o el SQLite local 'planadm.db' si no está definida).

Las contraseñas se guardan siempre con hash bcrypt (nunca en claro).

Comandos:
  list                               Lista los usuarios.
  add    <usuario> [--role admin|user] [--name "Nombre"]   Crea un usuario.
  passwd <usuario>                   Cambia la contraseña de un usuario.
  role   <usuario> <admin|user>      Cambia el rol.
  disable <usuario>                  Desactiva (bloquea el login).
  enable  <usuario>                  Reactiva.
  delete  <usuario>                  Elimina definitivamente.

La contraseña se pide de forma interactiva (no se escribe en el comando, para
que no quede en el historial de la terminal).

Ejemplos:
  python manage_users.py list
  python manage_users.py add jperez --role user --name "Juan Pérez"
  python manage_users.py passwd admin
  python manage_users.py role jperez admin

En Docker:
  docker compose exec app python manage_users.py list
  docker compose exec app python manage_users.py add jperez --role user
"""

from __future__ import annotations

import argparse
import getpass
import sys

import db
import app_auth


def _prompt_password() -> str:
    p1 = getpass.getpass("Nueva contraseña: ")
    if len(p1) < 8:
        print("La contraseña debe tener al menos 8 caracteres.")
        sys.exit(2)
    p2 = getpass.getpass("Repite la contraseña: ")
    if p1 != p2:
        print("Las contraseñas no coinciden.")
        sys.exit(2)
    return p1


def cmd_list() -> int:
    users = db.list_users()
    if not users:
        print("No hay usuarios. El primer arranque de la app crea el admin "
              "inicial (admin / planadm2026).")
        return 0
    print(f"{'USUARIO':<20} {'ROL':<8} {'ACTIVO':<7} {'ÚLTIMO LOGIN':<20} NOMBRE")
    print("-" * 80)
    for u in users:
        print(f"{u['username']:<20} {u['role']:<8} "
              f"{'sí' if u['active'] else 'NO':<7} "
              f"{(u['last_login'] or '-')[:19]:<20} {u['display_name']}")
    return 0


def cmd_add(args) -> int:
    if db.get_user(args.username):
        print(f"El usuario '{args.username}' ya existe. "
              f"Usa 'passwd' o 'role' para modificarlo.")
        return 1
    pwd = _prompt_password()
    ok = app_auth.create_user(args.username, pwd, role=args.role,
                              display_name=args.name or "")
    print("Usuario creado." if ok else "No se pudo crear el usuario "
          "(¿base de datos disponible?).")
    return 0 if ok else 1


def cmd_passwd(args) -> int:
    u = db.get_user(args.username)
    if not u:
        print(f"El usuario '{args.username}' no existe.")
        return 1
    pwd = _prompt_password()
    ok = app_auth.create_user(args.username, pwd, role=u["role"],
                              display_name=u.get("display_name", ""))
    print("Contraseña actualizada." if ok else "No se pudo actualizar.")
    return 0 if ok else 1


def cmd_role(args) -> int:
    u = db.get_user(args.username)
    if not u:
        print(f"El usuario '{args.username}' no existe.")
        return 1
    ok = db.upsert_user(args.username, u["password_hash"], role=args.role,
                        display_name=u.get("display_name", ""))
    print(f"Rol actualizado a '{args.role}'." if ok else "No se pudo actualizar.")
    return 0 if ok else 1


def cmd_active(args, active: bool) -> int:
    ok = db.set_user_active(args.username, active)
    if not ok:
        print(f"El usuario '{args.username}' no existe.")
        return 1
    print(f"Usuario {'activado' if active else 'desactivado'}.")
    return 0


def cmd_delete(args) -> int:
    confirm = input(f"¿Eliminar definitivamente a '{args.username}'? (escribe SI): ")
    if confirm.strip() != "SI":
        print("Cancelado.")
        return 0
    ok = db.delete_user(args.username)
    print("Usuario eliminado." if ok else f"El usuario '{args.username}' no existe.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestión de usuarios de Kyndryl Orbit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Lista los usuarios.")

    p_add = sub.add_parser("add", help="Crea un usuario.")
    p_add.add_argument("username")
    p_add.add_argument("--role", choices=["admin", "user"], default="user")
    p_add.add_argument("--name", default="", help="Nombre para mostrar.")

    p_pwd = sub.add_parser("passwd", help="Cambia la contraseña.")
    p_pwd.add_argument("username")

    p_role = sub.add_parser("role", help="Cambia el rol.")
    p_role.add_argument("username")
    p_role.add_argument("role", choices=["admin", "user"])

    p_dis = sub.add_parser("disable", help="Desactiva (bloquea el login).")
    p_dis.add_argument("username")

    p_en = sub.add_parser("enable", help="Reactiva un usuario.")
    p_en.add_argument("username")

    p_del = sub.add_parser("delete", help="Elimina definitivamente.")
    p_del.add_argument("username")

    args = parser.parse_args()
    db.init_db()

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "passwd":
        return cmd_passwd(args)
    if args.cmd == "role":
        return cmd_role(args)
    if args.cmd == "disable":
        return cmd_active(args, False)
    if args.cmd == "enable":
        return cmd_active(args, True)
    if args.cmd == "delete":
        return cmd_delete(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
