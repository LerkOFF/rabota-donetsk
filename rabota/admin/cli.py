"""Команда `python -m rabota make-admin <почта>`: первый администратор без пароля в аргументах."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime

from rabota.accounts import passwords, service
from rabota.admin import audit
from rabota.admin.common import iso, now_utc


def make_admin(conn: sqlite3.Connection, email: str, now: datetime | None = None) -> tuple[int, bool]:
    """Сделать пользователя администратором. Возвращает (id, создан ли аккаунт).

    Есть аккаунт: меняется только роль, пароль остаётся, почта считается подтверждённой.
    Нет аккаунта: создаётся с случайным паролем, который никому не известен; пароль задаётся через «Забыли пароль».
    """
    now = now or now_utc()
    email = service.normalize_email(email)
    if not service.valid_email(email):
        raise ValueError("Некорректный адрес почты.")
    row = service.get_user_by_email(conn, email)
    if row is not None:
        conn.execute(
            "UPDATE users SET role='admin', status='active', email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?",
            (iso(now), row["id"]),
        )
        conn.commit()
        service.delete_user_sessions(conn, row["id"])
        user_id, created = int(row["id"]), False
    else:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, role, name, email_verified_at, consent_at, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (email, passwords.hash_password(secrets.token_urlsafe(32)), "admin", "Администратор", iso(now), iso(now), iso(now)),
        )
        conn.commit()
        user_id, created = int(cur.lastrowid or 0), True
    audit.log(conn, None, "admin.make", "user", user_id, {"email": email, "created": created}, now=now)
    return user_id, created
