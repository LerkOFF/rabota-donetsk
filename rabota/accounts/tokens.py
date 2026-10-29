"""Одноразовые токены для писем и сессий. В базе лежит только SHA-256 хеш, сам токен знает владелец письма."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

PURPOSE_TTL = {
    "verify": timedelta(hours=24),
    "reset": timedelta(hours=1),
    "change": timedelta(hours=24),
}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def iso(moment: datetime) -> str:
    """Единый формат времени в базе: UTC, секунды. Строки сравниваются как время."""
    return moment.replace(microsecond=0).isoformat()


def create_email_token(
    conn: sqlite3.Connection, user_id: int, purpose: str, now: datetime, new_email: str | None = None
) -> str:
    """Выпустить токен. Прежние неиспользованные токены того же назначения гаснут: действует только последний."""
    if purpose not in PURPOSE_TTL:
        raise ValueError(f"неизвестное назначение токена: {purpose}")
    raw = new_token()
    conn.execute(
        "UPDATE email_tokens SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
        (iso(now), user_id, purpose),
    )
    conn.execute(
        "INSERT INTO email_tokens(user_id, token_hash, purpose, new_email, expires_at, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, hash_token(raw), purpose, new_email, iso(now + PURPOSE_TTL[purpose]), iso(now)),
    )
    conn.commit()
    return raw


def peek_email_token(conn: sqlite3.Connection, raw: str, purpose: str, now: datetime) -> dict[str, Any] | None:
    """Проверить токен, не расходуя его. None, если токена нет, он просрочен или уже использован."""
    if not raw or len(raw) > 200:
        return None
    row = conn.execute(
        "SELECT * FROM email_tokens WHERE token_hash=? AND purpose=? AND used_at IS NULL AND expires_at>?",
        (hash_token(raw), purpose, iso(now)),
    ).fetchone()
    return dict(row) if row else None


def consume_email_token(conn: sqlite3.Connection, raw: str, purpose: str, now: datetime) -> dict[str, Any] | None:
    """Израсходовать токен. Одним UPDATE: при двух одновременных запросах успех получит только один."""
    if not raw or len(raw) > 200:
        return None
    cur = conn.execute(
        "UPDATE email_tokens SET used_at=? WHERE token_hash=? AND purpose=? AND used_at IS NULL AND expires_at>?",
        (iso(now), hash_token(raw), purpose, iso(now)),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    row = conn.execute("SELECT * FROM email_tokens WHERE token_hash=?", (hash_token(raw),)).fetchone()
    return dict(row) if row else None


def purge_expired_tokens(conn: sqlite3.Connection, now: datetime) -> int:
    """Удалить просроченные и использованные токены старше суток."""
    cur = conn.execute(
        "DELETE FROM email_tokens WHERE expires_at<? OR (used_at IS NOT NULL AND used_at<?)",
        (iso(now), iso(now - timedelta(days=1))),
    )
    conn.commit()
    return cur.rowcount
