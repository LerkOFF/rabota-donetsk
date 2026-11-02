"""Шестизначные коды из писем: подтверждение почты при регистрации и сброс пароля.

В базе лежит только HMAC-SHA256 кода (ключ SECRET_KEY, в сообщение входят назначение и пользователь), сам код
знает владелец письма. Действует один последний код на пользователя и назначение: новый гасит прежний.
Код живёт недолго, на него даётся не больше MAX_ATTEMPTS попыток, выпуск ограничен по частоте.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from rabota.accounts.tokens import iso

CODE_LENGTH = 6
CODE_TTL = {"verify": timedelta(minutes=15), "reset": timedelta(minutes=10)}
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_PER_HOUR = 5

_CODE_RE = re.compile(rf"^[0-9]{{{CODE_LENGTH}}}$")


def new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def clean_code(raw: str) -> str | None:
    """Код из поля формы: пробелы, дефисы и точки (так цифры пишут в письмах) отбрасываются. None: это не код."""
    value = re.sub(r"[\s\-.]", "", raw or "")
    return value if _CODE_RE.match(value) else None


def hash_code(secret: bytes, user_id: int, purpose: str, code: str) -> str:
    msg = f"{purpose}:{user_id}:{code}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def issue_code(
    conn: sqlite3.Connection, secret: bytes, user_id: int, purpose: str, now: datetime, *, cooldown: bool = True
) -> str | None:
    """Выпустить код. None: слишком часто (пауза между письмами или лимит в час), письмо слать не нужно."""
    if purpose not in CODE_TTL:
        raise ValueError(f"неизвестное назначение кода: {purpose}")
    last = conn.execute(
        "SELECT MAX(created_at) AS t FROM email_codes WHERE user_id=? AND purpose=?", (user_id, purpose)
    ).fetchone()
    if cooldown and last["t"] and last["t"] > iso(now - RESEND_COOLDOWN):
        return None
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM email_codes WHERE user_id=? AND purpose=? AND created_at>?",
        (user_id, purpose, iso(now - timedelta(hours=1))),
    ).fetchone()["n"]
    if recent >= MAX_PER_HOUR:
        return None
    code = new_code()
    conn.execute(
        "UPDATE email_codes SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
        (iso(now), user_id, purpose),
    )
    conn.execute(
        "INSERT INTO email_codes(user_id, purpose, code_hash, expires_at, created_at) VALUES (?,?,?,?,?)",
        (user_id, purpose, hash_code(secret, user_id, purpose, code), iso(now + CODE_TTL[purpose]), iso(now)),
    )
    conn.commit()
    return code


def check_code(
    conn: sqlite3.Connection, secret: bytes, user_id: int, purpose: str, code: str, now: datetime
) -> str:
    """Проверить код и при успехе израсходовать. Ответ: ok, none (кода нет или он уже использован), expired,
    locked (попытки исчерпаны), bad. Каждая проверка тратит попытку; на последней неудаче код гаснет."""
    row = conn.execute(
        "SELECT * FROM email_codes WHERE user_id=? AND purpose=? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id, purpose),
    ).fetchone()
    if row is None:
        return "none"
    if row["expires_at"] <= iso(now):
        return "expired"
    spent = conn.execute(
        "UPDATE email_codes SET attempts=attempts+1 WHERE id=? AND used_at IS NULL AND attempts<?",
        (row["id"], MAX_ATTEMPTS),
    )
    conn.commit()
    if spent.rowcount != 1:
        return "locked"
    attempts = row["attempts"] + 1
    if hmac.compare_digest(row["code_hash"], hash_code(secret, user_id, purpose, code)):
        # одним UPDATE: из двух одновременных запросов с верным кодом успех получит один
        done = conn.execute("UPDATE email_codes SET used_at=? WHERE id=? AND used_at IS NULL", (iso(now), row["id"]))
        conn.commit()
        return "ok" if done.rowcount == 1 else "none"
    if attempts >= MAX_ATTEMPTS:
        conn.execute("UPDATE email_codes SET used_at=? WHERE id=? AND used_at IS NULL", (iso(now), row["id"]))
        conn.commit()
        return "locked"
    return "bad"


def burn_codes(conn: sqlite3.Connection, user_id: int, purpose: str, now: datetime) -> None:
    """Погасить все неиспользованные коды назначения (без commit: вызывающий коммитит сам)."""
    conn.execute(
        "UPDATE email_codes SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
        (iso(now), user_id, purpose),
    )


def purge_codes(conn: sqlite3.Connection, now: datetime) -> int:
    """Удалить просроченные коды и использованные старше суток."""
    cur = conn.execute(
        "DELETE FROM email_codes WHERE expires_at<? OR (used_at IS NOT NULL AND used_at<?)",
        (iso(now - timedelta(days=1)), iso(now - timedelta(days=1))),
    )
    conn.commit()
    return cur.rowcount
