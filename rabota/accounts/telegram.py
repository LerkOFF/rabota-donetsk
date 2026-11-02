"""Привязка Telegram к аккаунту и вход через бота. Без веба: функции получают соединение и время.

Аккаунт создаётся только через почту. Telegram привязывается в профиле: сайт выдаёт одноразовый код, человек
открывает t.me/<бот>?start=link_<код>, бот вызывает bind_by_code. После привязки на странице входа работает
кнопка "Войти через Telegram": страница заводит запрос на вход (start_login), человек открывает
t.me/<бот>?start=login_<код>, бот вызывает confirm_login, страница забирает сессию (complete_login).

Коды в базе только в виде SHA-256, сравнение через hmac.compare_digest, сами коды нигде не пишутся в журнал.
Код входа выводится из случайного значения в cookie браузера (HMAC с секретом сайта), поэтому чужой, узнавший
ссылку, сессию получить не может: у него нет значения из cookie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from rabota.accounts import service
from rabota.accounts.tokens import iso
from rabota.alerts import service as alerts_service

LINK_PREFIX = "link_"
LOGIN_PREFIX = "login_"
LINK_TTL = timedelta(minutes=15)
LOGIN_TTL = timedelta(minutes=5)
KEEP = timedelta(days=1)  # сколько хранятся погашенные и просроченные записи

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,100}$")


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def split_start(arg: str) -> tuple[str, str]:
    """Параметр /start: ("link", код), ("login", код) или ("", параметр) для остального (подписки на вакансии)."""
    if arg.startswith(LINK_PREFIX):
        return "link", arg[len(LINK_PREFIX):]
    if arg.startswith(LOGIN_PREFIX):
        return "login", arg[len(LOGIN_PREFIX):]
    return "", arg


def clean_username(value: object) -> str:
    text = str(value or "").lstrip("@")
    return text if _USERNAME_RE.match(text) else ""


def purge(conn: sqlite3.Connection, now: datetime) -> None:
    cutoff = iso(now - KEEP)
    conn.execute("DELETE FROM tg_link_codes WHERE expires_at<? OR (used_at IS NOT NULL AND used_at<?)", (cutoff, cutoff))
    conn.execute("DELETE FROM tg_login_requests WHERE expires_at<?", (cutoff,))
    conn.commit()


# ------------------------------------------------------------------ состояние привязки

def can_sign_in_without_telegram(row: sqlite3.Row | dict[str, Any]) -> bool:
    """Останется ли способ войти после отвязки: пароль или подтверждённая почта."""
    return bool(row["password_hash"]) or bool(row["email_verified_at"])


def link_info(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT tg_user_id, tg_username, tg_linked_at, password_hash, email_verified_at FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if row is None or not row["tg_user_id"]:
        return {"linked": False, "username": "", "linked_at": "", "can_unlink": False}
    return {
        "linked": True, "username": row["tg_username"] or "", "linked_at": row["tg_linked_at"] or "",
        "can_unlink": can_sign_in_without_telegram(row),
    }


# ------------------------------------------------------------------ привязка

def issue_link_code(conn: sqlite3.Connection, user_id: int, now: datetime) -> str:
    """Новый код привязки. Прежние неиспользованные гаснут: действует последний."""
    code = secrets.token_urlsafe(16)
    conn.execute("DELETE FROM tg_link_codes WHERE user_id=? AND used_at IS NULL", (user_id,))
    conn.execute(
        "INSERT INTO tg_link_codes(user_id, code_hash, expires_at, created_at) VALUES (?,?,?,?)",
        (user_id, hash_code(code), iso(now + LINK_TTL), iso(now)),
    )
    conn.commit()
    return code


def bind_by_code(
    conn: sqlite3.Connection, code: str, tg_user_id: int, tg_username: str, now: datetime
) -> tuple[str, dict[str, Any] | None]:
    """Привязать Telegram к аккаунту, выпустившему код. Ответ:
    ok, bad (кода нет, просрочен или уже использован), taken (этот Telegram у другого аккаунта),
    other (у аккаунта уже другой Telegram), blocked (аккаунт заблокирован)."""
    if not _CODE_RE.match(code or "") or tg_user_id <= 0:
        return "bad", None
    digest = hash_code(code)
    row = conn.execute("SELECT * FROM tg_link_codes WHERE code_hash=?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["code_hash"], digest):
        return "bad", None
    if row["used_at"] or row["expires_at"] <= iso(now):
        return "bad", None
    claimed = conn.execute(
        "UPDATE tg_link_codes SET used_at=? WHERE id=? AND used_at IS NULL", (iso(now), row["id"])
    )
    conn.commit()
    if claimed.rowcount != 1:
        return "bad", None
    user = service.get_user(conn, row["user_id"])
    if user is None:
        return "bad", None
    if user["status"] != "active":
        return "blocked", user
    if user.get("tg_user_id") and int(user["tg_user_id"]) != tg_user_id:
        return "other", user
    holder = conn.execute("SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,)).fetchone()
    if holder is not None and holder["id"] != user["id"]:
        return "taken", user
    try:
        conn.execute(
            "UPDATE users SET tg_user_id=?, tg_username=?, tg_linked_at=? WHERE id=?",
            (tg_user_id, clean_username(tg_username) or None, iso(now), user["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # гонка: этот Telegram успели привязать к другому аккаунту
        conn.rollback()
        return "taken", user
    alerts_service.adopt_telegram(conn, int(user["id"]), tg_user_id)  # подписки, оформленные до входа, теперь в аккаунте
    return "ok", service.get_user(conn, user["id"])


def unlink(conn: sqlite3.Connection, user_id: int, now: datetime) -> str:
    """Отвязать Telegram. Ответ: ok, not_linked, no_login (после отвязки нечем было бы войти)."""
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or not row["tg_user_id"]:
        return "not_linked"
    if not can_sign_in_without_telegram(row):
        return "no_login"
    conn.execute("UPDATE users SET tg_user_id=NULL, tg_username=NULL, tg_linked_at=NULL WHERE id=?", (user_id,))
    # незавершённые входы через этот Telegram больше не действуют
    conn.execute(
        "UPDATE tg_login_requests SET status='used', used_at=? WHERE user_id=? AND status IN ('confirmed','pending')",
        (iso(now), user_id),
    )
    conn.execute("DELETE FROM tg_link_codes WHERE user_id=?", (user_id,))
    conn.commit()
    return "ok"


# ------------------------------------------------------------------ вход

def new_nonce() -> str:
    """Значение для cookie браузера, который начал вход."""
    return secrets.token_urlsafe(32)


def code_for_nonce(secret: bytes, nonce: str) -> str:
    """Код входа для ссылки на бота. Выводится из значения в cookie, так что страница может показать ссылку снова."""
    digest = hmac.new(secret, f"tglogin:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]


def start_login(conn: sqlite3.Connection, secret: bytes, ip: str, now: datetime) -> tuple[str, str]:
    """Завести запрос на вход: (значение для cookie, код для ссылки t.me/<бот>?start=login_<код>)."""
    nonce = new_nonce()
    code = code_for_nonce(secret, nonce)
    purge(conn, now)
    conn.execute(
        "INSERT INTO tg_login_requests(code_hash, ip, created_at, expires_at) VALUES (?,?,?,?)",
        (hash_code(code), ip[:64], iso(now), iso(now + LOGIN_TTL)),
    )
    conn.commit()
    return nonce, code


def _request(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    digest = hash_code(code)
    row = conn.execute("SELECT * FROM tg_login_requests WHERE code_hash=?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["code_hash"], digest):
        return None
    return row  # type: ignore[no-any-return]


def request_of(conn: sqlite3.Connection, secret: bytes, nonce: str) -> sqlite3.Row | None:
    """Запрос на вход этого браузера по значению из cookie."""
    if not _NONCE_RE.match(nonce or ""):
        return None
    return _request(conn, code_for_nonce(secret, nonce))


def confirm_login(
    conn: sqlite3.Connection, code: str, tg_user_id: int, now: datetime
) -> tuple[str, dict[str, Any] | None]:
    """Бот получил /start login_<код> от человека tg_user_id. Ответ:
    ok, bad (кода нет или он просрочен), done (уже подтверждён), unlinked (Telegram ни к чему не привязан),
    blocked (аккаунт заблокирован или почта не подтверждена)."""
    if not _CODE_RE.match(code or "") or tg_user_id <= 0:
        return "bad", None
    row = _request(conn, code)
    if row is None or row["expires_at"] <= iso(now):
        return "bad", None
    if row["status"] in ("confirmed", "used"):
        return "done", None
    owner = conn.execute("SELECT * FROM users WHERE tg_user_id=?", (tg_user_id,)).fetchone()
    if owner is None:
        conn.execute("UPDATE tg_login_requests SET status='unlinked', tg_user_id=? WHERE id=?", (tg_user_id, row["id"]))
        conn.commit()
        return "unlinked", None
    user = service.public_user(owner)
    assert user is not None
    if owner["status"] != "active" or not owner["email_verified_at"]:
        conn.execute(
            "UPDATE tg_login_requests SET status='blocked', tg_user_id=?, user_id=? WHERE id=?",
            (tg_user_id, owner["id"], row["id"]),
        )
        conn.commit()
        return "blocked", user
    done = conn.execute(
        "UPDATE tg_login_requests SET status='confirmed', user_id=?, tg_user_id=?, confirmed_at=?"
        " WHERE id=? AND status IN ('pending','unlinked','blocked')",
        (owner["id"], tg_user_id, iso(now), row["id"]),
    )
    conn.commit()
    return ("ok", user) if done.rowcount == 1 else ("done", None)


def login_state(conn: sqlite3.Connection, secret: bytes, nonce: str, now: datetime) -> str:
    """Что показать странице входа: waiting, confirmed, unlinked, blocked, expired, none (запроса нет)."""
    row = request_of(conn, secret, nonce)
    if row is None:
        return "none"
    if row["status"] == "used" or row["expires_at"] <= iso(now):
        return "expired"
    return {"pending": "waiting"}.get(row["status"], row["status"])  # type: ignore[no-any-return]


def complete_login(
    conn: sqlite3.Connection, secret: bytes, nonce: str, now: datetime
) -> tuple[str, dict[str, Any] | None]:
    """Забрать вход: запрос гасится, пользователь возвращается один раз. Ответ как у login_state, плюс ok."""
    row = request_of(conn, secret, nonce)
    if row is None:
        return "none", None
    if row["status"] == "used" or row["expires_at"] <= iso(now):
        return "expired", None
    if row["status"] != "confirmed":
        return ("waiting" if row["status"] == "pending" else row["status"]), None
    taken = conn.execute(
        "UPDATE tg_login_requests SET status='used', used_at=? WHERE id=? AND status='confirmed' AND expires_at>?",
        (iso(now), row["id"], iso(now)),
    )
    conn.commit()
    if taken.rowcount != 1:
        return "expired", None
    owner = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if (
        owner is None or owner["status"] != "active" or not owner["email_verified_at"]
        or owner["tg_user_id"] != row["tg_user_id"]  # Telegram отвязали или перепривязали, пока шёл вход
    ):
        return "blocked", None
    return "ok", service.public_user(owner)


# ------------------------------------------------------------------ настройки

def config() -> Any:
    from rabota.alerts import conf

    return conf.TelegramConfig.from_env()


def available(conn: sqlite3.Connection | None) -> bool:
    """Бот настроен (токен и имя) и включён в настройках админки: привязка и вход через Telegram работают."""
    from rabota.alerts import conf

    try:
        return bool(config().bot_ready and conf.setting(conn, "tg_bot_enabled"))
    except Exception:  # noqa: BLE001 - страница входа не должна падать из-за настроек
        return False
