"""Логика аккаунтов без привязки к вебу: регистрация, подтверждение, вход, сессии, пароли, почта, удаление.

Все функции получают соединение SQLite и текущее время (UTC), поэтому проверяются в тестах без ожиданий.
Время в базе хранится строкой ISO (UTC, секунды), строки сравниваются как время.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rabota.accounts import passwords, tokens
from rabota.accounts.tokens import iso
from rabota.db import connect

ROLES = ("job_seeker", "employer")
ROLE_LABELS = {"job_seeker": "Соискатель", "employer": "Работодатель", "admin": "Администратор"}
SESSION_TTL = timedelta(days=30)
SESSION_REFRESH_AFTER = timedelta(days=1)
UNVERIFIED_KEEP = timedelta(days=7)

EMAIL_RE = re.compile(r"^[^\s@<>(),;:\"\\\[\]]{1,64}@[^\s@<>(),;:\"\\\[\]]+\.[^\s@<>(),;:\"\\\[\]]{2,}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

BLOCKED_TEXT = "Аккаунт заблокирован. Если это ошибка, напишите нам."


def ensure_schema(db_path: Path | str) -> None:
    connect(db_path).close()


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Соединение на один запрос: строки как словари, каскадное удаление включено."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ------------------------------------------------------------------ проверка ввода

def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and not CONTROL_RE.search(email) and bool(EMAIL_RE.match(email))


def clean_name(value: str) -> str:
    return " ".join(CONTROL_RE.sub(" ", value or "").split())[:80]


def normalize_phone(value: str) -> str | None:
    """Пустая строка допустима (телефон необязателен). None: номер не похож на телефон."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if re.search(r"[^\d\s+()\-.]", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if 10 <= len(digits) <= 15 and raw.startswith("+"):
        return "+" + digits
    return None


def validate_registration(
    email: str, password: str, name: str, role: str, consent: bool
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not valid_email(email):
        errors["email"] = "Проверьте адрес почты: он должен выглядеть как name@example.ru."
    if not name:
        errors["name"] = "Укажите, как к вам обращаться."
    if role not in ROLES:
        errors["role"] = "Выберите, кто вы: ищете работу или набираете сотрудников."
    problem = passwords.validate_password(password, email)
    if problem:
        errors["password"] = problem
    if not consent:
        errors["consent"] = "Без согласия на обработку персональных данных зарегистрироваться нельзя."
    return errors


# ------------------------------------------------------------------ пользователи

def public_user(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    """Пользователь для кода и шаблонов: без хеша пароля."""
    if row is None:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d["verified"] = bool(d.get("email_verified_at"))
    d["is_admin"] = d.get("role") == "admin"
    d["role_label"] = ROLE_LABELS.get(d.get("role", ""), "")
    return d


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    return public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email=?", (normalize_email(email),)).fetchone()


@dataclass
class Registration:
    kind: str  # new | pending | exists
    user_id: int
    name: str
    token: str | None = None  # токен подтверждения (для new и pending)


def register(
    conn: sqlite3.Connection, *, email: str, password: str, name: str, role: str, consent: bool, now: datetime
) -> Registration:
    """Создать аккаунт. Ввод уже проверен validate_registration.

    - new: аккаунт создан, нужно письмо со ссылкой подтверждения;
    - pending: почта занята, но не подтверждена: данные заменяются последней регистрацией (чтобы тот, кто занял
      чужую почту раньше хозяина, не получил доступ), выпускается новая ссылка;
    - exists: почта подтверждена, ничего не меняется, нужно письмо "вы уже зарегистрированы".
    """
    email = normalize_email(email)
    pw_hash = passwords.hash_password(password)  # считаем всегда: время ответа не выдаёт, занята ли почта
    existing = get_user_by_email(conn, email)
    if existing is not None and existing["email_verified_at"]:
        return Registration("exists", existing["id"], existing["name"])
    consent_at = iso(now) if consent else None
    if existing is not None:
        conn.execute(
            "UPDATE users SET password_hash=?, name=?, role=?, consent_at=? WHERE id=? AND email_verified_at IS NULL",
            (pw_hash, name, role, consent_at, existing["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
        raw = tokens.create_email_token(conn, existing["id"], "verify", now)
        return Registration("pending", existing["id"], name, raw)
    try:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, role, name, consent_at, created_at) VALUES (?,?,?,?,?,?)",
            (email, pw_hash, role, name, consent_at, iso(now)),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # гонка двух регистраций с одной почтой
        conn.rollback()
        again = get_user_by_email(conn, email)
        if again is None:
            raise
        return Registration("exists", again["id"], again["name"])
    user_id = int(cur.lastrowid or 0)
    return Registration("new", user_id, name, tokens.create_email_token(conn, user_id, "verify", now))


def resend_verification(conn: sqlite3.Connection, email: str, now: datetime) -> tuple[dict[str, Any], str] | None:
    """Новая ссылка подтверждения для неподтверждённого аккаунта. None, если отправлять нечего."""
    row = get_user_by_email(conn, email)
    if row is None or row["email_verified_at"]:
        return None
    return public_user(row), tokens.create_email_token(conn, row["id"], "verify", now)


def verify_email(conn: sqlite3.Connection, raw_token: str, now: datetime) -> dict[str, Any] | None:
    tok = tokens.consume_email_token(conn, raw_token, "verify", now)
    if tok is None:
        return None
    conn.execute(
        "UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?", (iso(now), tok["user_id"])
    )
    conn.commit()
    return get_user(conn, tok["user_id"])


# ------------------------------------------------------------------ вход

@dataclass
class AuthResult:
    status: str  # ok | bad | unverified | blocked
    user: dict[str, Any] | None = None


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> AuthResult:
    """Проверить почту и пароль. Про блокировку и неподтверждённую почту говорим только после верного пароля."""
    row = get_user_by_email(conn, email)
    if row is None or len(password) > passwords.MAX_LENGTH:
        passwords.burn_verify(password[: passwords.MAX_LENGTH])
        return AuthResult("bad")
    if not passwords.verify_password(password, row["password_hash"]):
        return AuthResult("bad")
    user = public_user(row)
    if row["status"] == "blocked":
        return AuthResult("blocked", user)
    if not row["email_verified_at"]:
        return AuthResult("unverified", user)
    if passwords.needs_rehash(row["password_hash"]):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (passwords.hash_password(password), row["id"]))
        conn.commit()
    return AuthResult("ok", user)


# ------------------------------------------------------------------ сессии

def create_session(
    conn: sqlite3.Connection, user_id: int, now: datetime, ip: str = "", user_agent: str = ""
) -> str:
    """Новая сессия. Токен возвращается один раз, в базе только его хеш."""
    raw = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(user_id, token_hash, ip, user_agent, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (user_id, tokens.hash_token(raw), ip[:64], user_agent[:200], iso(now), iso(now + SESSION_TTL)),
    )
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(now), user_id))
    conn.commit()
    return raw


def resolve_session(
    conn: sqlite3.Connection, raw: str, now: datetime
) -> tuple[dict[str, Any] | None, datetime | None]:
    """Пользователь по токену сессии и новый срок, если сессию продлили (тогда cookie надо обновить)."""
    if not raw or len(raw) > 200:
        return None, None
    row = conn.execute(
        "SELECT s.id AS sid, s.expires_at AS s_expires, u.* FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token_hash=?",
        (tokens.hash_token(raw),),
    ).fetchone()
    if row is None:
        return None, None
    if row["s_expires"] <= iso(now):
        conn.execute("DELETE FROM sessions WHERE id=?", (row["sid"],))
        conn.commit()
        return None, None
    if row["status"] != "active" or not row["email_verified_at"]:
        return None, None
    fields = {k: row[k] for k in row.keys() if k not in ("sid", "s_expires")}  # noqa: SIM118
    user = public_user(fields)
    new_expiry = None
    if row["s_expires"] < iso(now + SESSION_TTL - SESSION_REFRESH_AFTER):
        new_expiry = now + SESSION_TTL
        conn.execute("UPDATE sessions SET expires_at=? WHERE id=?", (iso(new_expiry), row["sid"]))
        conn.commit()
    return user, new_expiry


def delete_session(conn: sqlite3.Connection, raw: str) -> None:
    if raw:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (tokens.hash_token(raw),))
        conn.commit()


def delete_user_sessions(conn: sqlite3.Connection, user_id: int, keep_raw: str | None = None) -> None:
    """Закрыть все сессии пользователя, кроме одной (текущей), если она указана."""
    if keep_raw:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, tokens.hash_token(keep_raw)))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()


def purge_expired(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (iso(now),))
    conn.commit()
    tokens.purge_expired_tokens(conn, now)


def purge_unverified(conn: sqlite3.Connection, now: datetime, keep: timedelta = UNVERIFIED_KEEP) -> int:
    """Удалить аккаунты, которые так и не подтвердили почту за неделю."""
    cur = conn.execute(
        "DELETE FROM users WHERE email_verified_at IS NULL AND created_at<?", (iso(now - keep),)
    )
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ пароль

def change_password(
    conn: sqlite3.Connection, user_id: int, new_password: str, now: datetime, keep_session: str | None = None
) -> None:
    """Сменить пароль, закрыть остальные сессии и погасить выданные ранее ссылки сброса."""
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (passwords.hash_password(new_password), user_id))
    conn.execute(
        "UPDATE email_tokens SET used_at=? WHERE user_id=? AND purpose='reset' AND used_at IS NULL",
        (iso(now), user_id),
    )
    conn.commit()
    delete_user_sessions(conn, user_id, keep_session)


def request_reset(conn: sqlite3.Connection, email: str, now: datetime) -> tuple[dict[str, Any], str] | None:
    """Ссылка для сброса пароля. None для неизвестной почты и заблокированного аккаунта."""
    row = get_user_by_email(conn, email)
    if row is None or row["status"] != "active":
        return None
    return public_user(row), tokens.create_email_token(conn, row["id"], "reset", now)


def reset_password(conn: sqlite3.Connection, raw_token: str, new_password: str, now: datetime) -> dict[str, Any] | None:
    """Задать новый пароль по ссылке. Все сессии закрываются. Ссылка из письма подтверждает и почту."""
    tok = tokens.consume_email_token(conn, raw_token, "reset", now)
    if tok is None:
        return None
    conn.execute(
        "UPDATE users SET email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?", (iso(now), tok["user_id"])
    )
    change_password(conn, tok["user_id"], new_password, now)
    return get_user(conn, tok["user_id"])


# ------------------------------------------------------------------ профиль и почта

def update_profile(conn: sqlite3.Connection, user_id: int, name: str, phone: str) -> None:
    conn.execute("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone or None, user_id))
    conn.commit()


def check_password(conn: sqlite3.Connection, user_id: int, password: str) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or len(password) > passwords.MAX_LENGTH:
        return False
    return passwords.verify_password(password, row["password_hash"])


def request_email_change(
    conn: sqlite3.Connection, user_id: int, new_email: str, now: datetime
) -> str | None:
    """Токен подтверждения нового адреса. None, если адрес уже занят (снаружи отвечаем одинаково)."""
    new_email = normalize_email(new_email)
    if get_user_by_email(conn, new_email) is not None:
        return None
    return tokens.create_email_token(conn, user_id, "change", now, new_email=new_email)


def confirm_email_change(conn: sqlite3.Connection, raw_token: str, now: datetime) -> tuple[dict[str, Any], str] | None:
    """Применить смену почты. Возвращает (пользователь с новой почтой, старая почта) или None."""
    tok = tokens.consume_email_token(conn, raw_token, "change", now)
    if tok is None or not tok["new_email"]:
        return None
    old = conn.execute("SELECT email FROM users WHERE id=?", (tok["user_id"],)).fetchone()
    if old is None:
        return None
    try:
        conn.execute(
            "UPDATE users SET email=?, email_verified_at=? WHERE id=?", (tok["new_email"], iso(now), tok["user_id"])
        )
        conn.execute(
            "UPDATE email_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (iso(now), tok["user_id"])
        )
        conn.commit()
    except sqlite3.IntegrityError:  # адрес заняли, пока шло письмо
        conn.rollback()
        return None
    delete_user_sessions(conn, tok["user_id"])
    user = get_user(conn, tok["user_id"])
    return (user, old["email"]) if user else None


# ------------------------------------------------------------------ удаление, блокировка, администратор

def delete_account(conn: sqlite3.Connection, user_id: int) -> None:
    """Удалить аккаунт и всё, что с ним связано: сессии, резюме, отклики, переписку, компании и вакансии владельца."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM vacancies WHERE owner_user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()


def set_blocked(conn: sqlite3.Connection, user_id: int, blocked: bool) -> None:
    """Заблокировать или разблокировать. Блокировка сразу закрывает все сессии."""
    conn.execute("UPDATE users SET status=? WHERE id=?", ("blocked" if blocked else "active", user_id))
    conn.commit()
    if blocked:
        delete_user_sessions(conn, user_id)


def create_admin(conn: sqlite3.Connection, email: str, password: str, now: datetime | None = None) -> int:
    """Создать администратора (почта подтверждена сразу). Если такая почта есть, аккаунт становится администратором
    с новым паролем. Возвращает id. ValueError с понятным текстом при плохой почте или слабом пароле."""
    now = now or datetime.now(UTC)
    email = normalize_email(email)
    if not valid_email(email):
        raise ValueError("Некорректный адрес почты.")
    problem = passwords.validate_password(password, email)
    if problem:
        raise ValueError(problem)
    pw_hash = passwords.hash_password(password)
    row = get_user_by_email(conn, email)
    if row is not None:
        conn.execute(
            "UPDATE users SET role='admin', password_hash=?, status='active',"
            " email_verified_at=COALESCE(email_verified_at, ?) WHERE id=?",
            (pw_hash, iso(now), row["id"]),
        )
        conn.commit()
        delete_user_sessions(conn, row["id"])
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO users(email, password_hash, role, name, email_verified_at, consent_at, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (email, pw_hash, "admin", "Администратор", iso(now), iso(now), iso(now)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)
