"""Журнал действий администратора: кто, что, когда, над чем. Записи не правятся и не удаляются из интерфейса."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from rabota.admin.common import Page, actor_id, clamp_page, iso, now_utc

SYSTEM = "система"
_SECRET_KEYS = ("password", "pass", "token", "csrf", "secret")


def scrub(details: Any) -> Any:
    """Из деталей убираются пароли и токены: журнал их хранить не должен."""
    if isinstance(details, dict):
        return {k: scrub(v) for k, v in details.items() if not any(s in str(k).lower() for s in _SECRET_KEYS)}
    if isinstance(details, list | tuple):
        return [scrub(v) for v in details]
    return details


def log(
    conn: sqlite3.Connection,
    actor: dict[str, Any] | None,
    action: str,
    target_type: str = "",
    target_id: Any = "",
    details: Any = None,
    ip: str = "",
    now: datetime | None = None,
) -> int:
    text = details if isinstance(details, str) else json.dumps(scrub(details or {}), ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO audit_log(at, actor_user_id, actor_email, action, target_type, target_id, details, ip)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (iso(now or now_utc()), actor_id(actor), (actor or {}).get("email") or SYSTEM, action, target_type,
         str(target_id), text[:2000], ip[:64]),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def count(conn: sqlite3.Connection, action_prefix: str = "") -> int:
    if action_prefix:
        return int(conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE ?", (action_prefix + "%",)).fetchone()[0])
    return int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])


def recent(conn: sqlite3.Connection, limit: int = 50, offset: int = 0, action_prefix: str = "") -> list[dict[str, Any]]:
    where, args = ("WHERE action LIKE ?", [action_prefix + "%"]) if action_prefix else ("", [])
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset]
    ).fetchall()
    return [dict(r) for r in rows]


def page(conn: sqlite3.Connection, page_no: int = 1, action_prefix: str = "", per_page: int = 50) -> Page:
    total = count(conn, action_prefix)
    p = clamp_page(page_no, total, per_page)
    return Page(recent(conn, per_page, (p - 1) * per_page, action_prefix), total, p, per_page)
