"""Создать администратора сайта (или сделать администратором существующий аккаунт).

    .venv/bin/python scripts/create_admin.py admin@example.ru

Пароль спрашивается без показа на экране и нигде не печатается. Почта подтверждается сразу.
База берётся из DB_PATH (.env), либо опцией --db.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rabota.accounts.service import create_admin, get_user_by_email, normalize_email
from rabota.db import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Создать администратора")
    parser.add_argument("email", help="почта администратора")
    parser.add_argument("--db", help="путь к базе SQLite (по умолчанию DB_PATH из .env)")
    args = parser.parse_args(argv)

    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают.", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        existed = get_user_by_email(conn, normalize_email(args.email)) is not None
        try:
            user_id = create_admin(conn, args.email, password)
        except ValueError as exc:
            print(f"Не получилось: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"Аккаунт {normalize_email(args.email)} {'обновлён до администратора' if existed else 'создан'} (id {user_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
