"""Подписки на вакансии без веба: создание, подтверждение кодом и ссылкой, привязка Telegram, отписка, лимиты, очистка.

Подписка принадлежит аккаунту (`user_id`) или гостю (только подтверждённая почта). Гость подтверждает почту
шестизначным кодом или ссылкой из письма, подписка в Telegram подтверждается самим входом в бота по одноразовому
коду. Вакансии, которые были на сайте до подтверждения, в рассылку не попадают: считаются только новые
(`baseline_id`). Повторно вакансия по подписке не уходит (таблица `alert_sent`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from rabota.accounts import service as accounts
from rabota.accounts.tokens import iso
from rabota.alerts import conf, filters

CHANNELS = ("email", "telegram")
FREQUENCIES = ("instant", "daily")
FREQUENCY_LABELS = {"instant": "сразу", "daily": "раз в день"}
STATUS_LABELS = {"pending": "ждёт подтверждения", "active": "включена", "paused": "на паузе", "unsubscribed": "отключена"}

MAX_PER_EMAIL = 5          # неподтверждённых и рабочих подписок на одну почту гостя
MAX_PER_CHAT = 10          # подписок на один чат Telegram
CODE_TTL = timedelta(minutes=30)
CODE_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
CONFIRM_PER_HOUR = 5       # писем с кодом на одну подписку в час
LINK_TTL = timedelta(hours=48)
TG_CODE_TTL = timedelta(hours=24)
PENDING_KEEP = timedelta(days=2)
GUEST_UNSUB_KEEP = timedelta(days=30)
LOG_KEEP = timedelta(days=60)
SENT_KEEP = timedelta(days=60)
POST_KEEP = timedelta(days=90)
MAX_TOKEN = 80

_TG_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class AlertError(Exception):
    """Понятная пользователю причина отказа. code: empty, limit, email, channel, frequency, closed, forbidden."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


# ------------------------------------------------------------------ вспомогательное

def max_vacancy_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM vacancies").fetchone()[0])


def get(conn: sqlite3.Connection, sub_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alert_subscriptions WHERE id = ?", (sub_id,)).fetchone()  # type: ignore[no-any-return]


def filters_of(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(row["filters"] or "{}")
    except (TypeError, ValueError):
        return {}
    return filters.normalize(data) if isinstance(data, dict) else {}


def check_options(channel: str, frequency: str) -> None:
    if channel not in CHANNELS:
        raise AlertError("channel", "Выберите, куда присылать вакансии: на почту или в Telegram.")
    if frequency not in FREQUENCIES:
        raise AlertError("frequency", "Выберите, как часто присылать: сразу или раз в день.")


def _check_filters(data: Mapping[str, Any]) -> dict[str, Any]:
    normalized = filters.normalize(data)
    if not normalized:
        raise AlertError("empty", "Сначала задайте поиск или фильтры: подписка на все вакансии подряд не нужна.")
    return normalized


def _insert(
    conn: sqlite3.Connection, *, user_id: int | None, email: str, channel: str, frequency: str, data: Mapping[str, Any],
    status: str, now: datetime,
) -> int:
    cur = conn.execute(
        "INSERT INTO alert_subscriptions(user_id, email, channel, frequency, filters, filters_key, title, status,"
        " baseline_id, created_at, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, email, channel, frequency, json.dumps(data, ensure_ascii=False, sort_keys=True),
            filters.key_of(data), filters.describe(data), status, max_vacancy_id(conn), iso(now),
            iso(now) if status == "active" else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _own_sql(user: Mapping[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Подписки аккаунта: свои и гостевые почтовые на его подтверждённую почту."""
    return (
        "(user_id = ? OR (user_id IS NULL AND channel = 'email' AND email = ? AND email != ''))",
        (user["id"], accounts.normalize_email(user.get("email") or "")),
    )


# ------------------------------------------------------------------ токены ссылок

def _sign(sub_id: int, purpose: str, secret: bytes) -> str:
    return hmac.new(secret, f"alert:{purpose}:{sub_id}".encode(), hashlib.sha256).hexdigest()[:32]


def make_token(sub_id: int, purpose: str, secret: bytes) -> str:
    """purpose: unsub (отписка из письма), confirm (ссылка подтверждения), ref (ссылка на страницу ввода кода)."""
    return f"{sub_id}.{_sign(sub_id, purpose, secret)}"


def parse_token(token: str, purpose: str, secret: bytes) -> int | None:
    if not token or len(token) > MAX_TOKEN:
        return None
    head, _, sig = token.partition(".")
    if not head.isascii() or not head.isdigit() or len(head) > 12 or not sig:
        return None
    sub_id = int(head)
    return sub_id if hmac.compare_digest(_sign(sub_id, purpose, secret), sig) else None


# ------------------------------------------------------------------ создание

def count_for_user(conn: sqlite3.Connection, user: Mapping[str, Any]) -> int:
    sql, args = _own_sql(user)
    return int(conn.execute(
        f"SELECT COUNT(DISTINCT filters_key) FROM alert_subscriptions WHERE {sql} AND status != 'unsubscribed'", args
    ).fetchone()[0])


def list_for_user(conn: sqlite3.Connection, user: Mapping[str, Any]) -> list[sqlite3.Row]:
    sql, args = _own_sql(user)
    return conn.execute(f"SELECT * FROM alert_subscriptions WHERE {sql} ORDER BY id DESC", args).fetchall()  # type: ignore[no-any-return]


def siblings(conn: sqlite3.Connection, user: Mapping[str, Any], row: sqlite3.Row) -> list[sqlite3.Row]:
    """Все каналы одной подписки: строки владельца с тем же поиском (почта первой)."""
    sql, args = _own_sql(user)
    return conn.execute(  # type: ignore[no-any-return]
        f"SELECT * FROM alert_subscriptions WHERE {sql} AND filters_key = ?"
        " ORDER BY CASE channel WHEN 'email' THEN 0 ELSE 1 END, id", (*args, row["filters_key"]),
    ).fetchall()


def list_groups(conn: sqlite3.Connection, user: Mapping[str, Any]) -> list[list[sqlite3.Row]]:
    """Подписки для кабинета: один поиск с почтой и Telegram это одна подписка. Новые сверху, внутри почта первой."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in list_for_user(conn, user):
        groups.setdefault(row["filters_key"], []).append(row)
    return [sorted(legs, key=lambda r: (r["channel"] != "email", r["id"])) for legs in groups.values()]


def get_owned(conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int) -> sqlite3.Row | None:
    sql, args = _own_sql(user)
    return conn.execute(f"SELECT * FROM alert_subscriptions WHERE id = ? AND {sql}", (sub_id, *args)).fetchone()  # type: ignore[no-any-return]


def _reactivate(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> sqlite3.Row:
    conn.execute(
        "UPDATE alert_subscriptions SET status = 'active', baseline_id = ?, error_count = 0, last_error = '',"
        " unsubscribed_at = NULL, confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ?",
        (max_vacancy_id(conn), iso(now), row["id"]),
    )
    conn.commit()
    return get(conn, row["id"])  # type: ignore[return-value]


def subscribe_user(
    conn: sqlite3.Connection, user: Mapping[str, Any], data: Mapping[str, Any], *, channel: str = "email",
    frequency: str = "daily", now: datetime,
) -> tuple[sqlite3.Row, bool]:
    """Подписка вошедшего (почта уже подтверждена). Возвращает (подписка, создана ли новая).
    Такая же подписка не дублируется: отключённая включается заново. Подписка в Telegram остаётся ждать привязки чата."""
    check_options(channel, frequency)
    normalized = _check_filters(data)
    sql, args = _own_sql(user)
    key = filters.key_of(normalized)
    existing = conn.execute(
        f"SELECT * FROM alert_subscriptions WHERE {sql} AND channel = ? AND filters_key = ? ORDER BY id LIMIT 1",
        (*args, channel, key),
    ).fetchone()
    if existing is not None:
        if existing["status"] in ("paused", "unsubscribed") and (channel == "email" or existing["tg_chat_id"]):
            existing = _reactivate(conn, existing, now)
        return existing, False
    has_search = conn.execute(
        f"SELECT 1 FROM alert_subscriptions WHERE {sql} AND filters_key = ? AND status != 'unsubscribed' LIMIT 1", (*args, key)
    ).fetchone()
    if not has_search and count_for_user(conn, user) >= int(conf.setting(conn, "alerts_max_per_user")):
        raise AlertError("limit", f"Можно держать не больше {conf.setting(conn, 'alerts_max_per_user')} подписок. "
                                  "Удалите ненужную в разделе «Подписки на вакансии».")
    status = "active" if channel == "email" else "pending"
    sub_id = _insert(conn, user_id=int(user["id"]), email="", channel=channel, frequency=frequency, data=normalized,
                     status=status, now=now)
    return get(conn, sub_id), True  # type: ignore[return-value]


def subscribe_guest(
    conn: sqlite3.Connection, email: str, data: Mapping[str, Any], *, frequency: str = "daily", secret: bytes, now: datetime,
) -> tuple[sqlite3.Row, str | None]:
    """Подписка гостя по почте. Возвращает (подписка, код для письма или None). None: письмо слать не нужно
    (подписка уже работает, слишком частая повторная отправка). Ответ для уже работающей подписки не отличается
    от нового запроса, чтобы по форме нельзя было узнать, есть ли такая почта в рассылке."""
    check_options("email", frequency)
    address = accounts.normalize_email(email)
    if not accounts.valid_email(address):
        raise AlertError("email", "Проверьте адрес почты: он указан с ошибкой.")
    normalized = _check_filters(data)
    key = filters.key_of(normalized)
    existing = conn.execute(
        "SELECT * FROM alert_subscriptions WHERE user_id IS NULL AND channel = 'email' AND email = ? AND filters_key = ?"
        " ORDER BY id LIMIT 1", (address, key),
    ).fetchone()
    if existing is not None:
        if existing["status"] == "active":
            return existing, None
        conn.execute("UPDATE alert_subscriptions SET status = 'pending', frequency = ? WHERE id = ?", (frequency, existing["id"]))
        conn.commit()
        return get(conn, existing["id"]), issue_code(conn, existing["id"], secret, now)  # type: ignore[return-value]
    busy = conn.execute(
        "SELECT COUNT(*) FROM alert_subscriptions WHERE user_id IS NULL AND channel = 'email' AND email = ?"
        " AND status != 'unsubscribed'", (address,),
    ).fetchone()[0]
    if busy >= MAX_PER_EMAIL:
        raise AlertError("limit", f"На одну почту можно оформить не больше {MAX_PER_EMAIL} подписок. "
                                  "Отключите ненужные по ссылке из письма.")
    sub_id = _insert(conn, user_id=None, email=address, channel="email", frequency=frequency, data=normalized,
                     status="pending", now=now)
    return get(conn, sub_id), issue_code(conn, sub_id, secret, now)  # type: ignore[return-value]


# ------------------------------------------------------------------ подтверждение почты

def _code_hash(secret: bytes, sub_id: int, code: str) -> str:
    return hmac.new(secret, f"alertcode:{sub_id}:{code}".encode(), hashlib.sha256).hexdigest()


def log_delivery(
    conn: sqlite3.Connection, now: datetime, *, channel: str, kind: str, ok: bool = True, error: str = "",
    subscription_id: int | None = None, vacancies: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO alert_log(at, subscription_id, channel, kind, ok, error, vacancies) VALUES (?,?,?,?,?,?,?)",
        (iso(now), subscription_id, channel, kind, 1 if ok else 0, error[:200], vacancies),
    )
    conn.commit()


def issue_code(conn: sqlite3.Connection, sub_id: int, secret: bytes, now: datetime) -> str | None:
    """Новый шестизначный код. None: пауза между письмами (минута) или больше пяти писем за час."""
    last = conn.execute(
        "SELECT MAX(at) FROM alert_log WHERE subscription_id = ? AND kind = 'confirm' AND ok = 1", (sub_id,)
    ).fetchone()[0]
    if last and last > iso(now - RESEND_COOLDOWN):
        return None
    recent = conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE subscription_id = ? AND kind = 'confirm' AND ok = 1 AND at > ?",
        (sub_id, iso(now - timedelta(hours=1))),
    ).fetchone()[0]
    if recent >= CONFIRM_PER_HOUR:
        return None
    code = f"{secrets.randbelow(10 ** 6):06d}"
    conn.execute(
        "UPDATE alert_subscriptions SET code_hash = ?, code_expires_at = ?, code_attempts = 0 WHERE id = ?",
        (_code_hash(secret, sub_id, code), iso(now + CODE_TTL), sub_id),
    )
    conn.commit()
    log_delivery(conn, now, channel="email", kind="confirm", subscription_id=sub_id)
    return code


def clean_code(raw: str) -> str | None:
    value = re.sub(r"[\s\-.]", "", raw or "")
    return value if re.fullmatch(r"[0-9]{6}", value) else None


def activate(conn: sqlite3.Connection, sub_id: int, now: datetime) -> sqlite3.Row | None:
    """Включить подписку после подтверждения: с этого момента считаются только новые вакансии."""
    conn.execute(
        "UPDATE alert_subscriptions SET status = 'active', confirmed_at = ?, baseline_id = ?, code_hash = '',"
        " code_expires_at = NULL, error_count = 0, last_error = '', unsubscribed_at = NULL"
        " WHERE id = ? AND status IN ('pending', 'paused', 'unsubscribed')",
        (iso(now), max_vacancy_id(conn), sub_id),
    )
    conn.commit()
    return get(conn, sub_id)


def check_code(conn: sqlite3.Connection, sub_id: int, raw: str, secret: bytes, now: datetime) -> str:
    """ok, none (подписки нет, она уже работает или не ждёт код), expired, locked (попытки исчерпаны), bad."""
    row = get(conn, sub_id)
    if row is None or row["channel"] != "email":
        return "none"
    if row["status"] != "pending" or not row["code_hash"]:
        return "none"  # в том числе уже работающая подписка: по коду нельзя узнать, есть ли такая почта в рассылке
    if not row["code_expires_at"] or row["code_expires_at"] <= iso(now):
        return "expired"
    spent = conn.execute(
        "UPDATE alert_subscriptions SET code_attempts = code_attempts + 1 WHERE id = ? AND code_attempts < ?",
        (sub_id, CODE_ATTEMPTS),
    )
    conn.commit()
    if spent.rowcount != 1:
        return "locked"
    code = clean_code(raw)
    if code and hmac.compare_digest(row["code_hash"], _code_hash(secret, sub_id, code)):
        activate(conn, sub_id, now)
        return "ok"
    if row["code_attempts"] + 1 >= CODE_ATTEMPTS:
        conn.execute("UPDATE alert_subscriptions SET code_hash = '' WHERE id = ?", (sub_id,))
        conn.commit()
        return "locked"
    return "bad"


def confirm_by_link(conn: sqlite3.Connection, sub_id: int, now: datetime) -> bool:
    """Подтверждение по ссылке из письма. Уже работающая подписка подтверждается без изменений."""
    row = get(conn, sub_id)
    if row is None or row["channel"] != "email":
        return False
    if row["status"] == "active":
        return True
    if row["status"] != "pending" or row["created_at"] <= iso(now - LINK_TTL):
        return False
    activate(conn, sub_id, now)
    return True


# ------------------------------------------------------------------ Telegram

def _tg_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_tg_code(conn: sqlite3.Connection, sub_id: int, now: datetime) -> str:
    """Одноразовый код для ссылки t.me/<бот>?start=<код>. В базе только хеш."""
    code = secrets.token_urlsafe(9)
    conn.execute(
        "UPDATE alert_subscriptions SET tg_code_hash = ?, tg_code_expires_at = ? WHERE id = ?",
        (_tg_hash(code), iso(now + TG_CODE_TTL), sub_id),
    )
    conn.commit()
    return code


def subscribe_telegram(
    conn: sqlite3.Connection, user: Mapping[str, Any] | None, data: Mapping[str, Any], *, frequency: str = "instant",
    now: datetime,
) -> tuple[sqlite3.Row, str | None]:
    """Подписка в Telegram: (подписка, код привязки). Код None, если чат уже привязан."""
    check_options("telegram", frequency)
    normalized = _check_filters(data)
    if user is not None:
        row, created = subscribe_user(conn, user, normalized, channel="telegram", frequency=frequency, now=now)
        if not created and row["tg_chat_id"]:
            return row, None
    else:
        sub_id = _insert(conn, user_id=None, email="", channel="telegram", frequency=frequency, data=normalized,
                         status="pending", now=now)
        row = get(conn, sub_id)  # type: ignore[assignment]
    return row, issue_tg_code(conn, row["id"], now)


def bind_telegram(conn: sqlite3.Connection, code: str, chat_id: int, now: datetime) -> tuple[str, sqlite3.Row | None]:
    """Привязать чат по коду из /start. Ответ: ok, bad (нет такого кода или он просрочен), limit (в чате много подписок)."""
    if not _TG_CODE_RE.match(code or ""):
        return "bad", None
    row = conn.execute(
        "SELECT * FROM alert_subscriptions WHERE tg_code_hash = ? AND channel = 'telegram'", (_tg_hash(code),)
    ).fetchone()
    if row is None or not row["tg_code_expires_at"] or row["tg_code_expires_at"] <= iso(now):
        return "bad", None
    busy = conn.execute(
        "SELECT COUNT(*) FROM alert_subscriptions WHERE tg_chat_id = ? AND status IN ('active', 'paused') AND id != ?",
        (chat_id, row["id"]),
    ).fetchone()[0]
    if busy >= MAX_PER_CHAT:
        return "limit", row
    conn.execute(
        "UPDATE alert_subscriptions SET tg_chat_id = ?, tg_code_hash = '', tg_code_expires_at = NULL WHERE id = ?",
        (chat_id, row["id"]),
    )
    conn.commit()
    return "ok", activate(conn, row["id"], now)


def subs_for_chat(conn: sqlite3.Connection, chat_id: int) -> list[sqlite3.Row]:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT * FROM alert_subscriptions WHERE tg_chat_id = ? AND channel = 'telegram' AND status IN ('active', 'paused')"
        " ORDER BY id", (chat_id,),
    ).fetchall()


def stop_chat(conn: sqlite3.Connection, chat_id: int, now: datetime, reason: str = "") -> int:
    """Отключить все подписки чата (команда /stop или бот заблокирован). Возвращает число отключённых."""
    cur = conn.execute(
        "UPDATE alert_subscriptions SET status = 'unsubscribed', unsubscribed_at = ?, last_error = ?"
        " WHERE tg_chat_id = ? AND channel = 'telegram' AND status IN ('active', 'paused')",
        (iso(now), reason[:200], chat_id),
    )
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ управление

def unsubscribe(conn: sqlite3.Connection, sub_id: int, now: datetime) -> sqlite3.Row | None:
    """Отписка по ссылке из письма (без входа). Повтор безопасен."""
    row = get(conn, sub_id)
    if row is None:
        return None
    if row["status"] != "unsubscribed":
        conn.execute(
            "UPDATE alert_subscriptions SET status = 'unsubscribed', unsubscribed_at = ? WHERE id = ?", (iso(now), sub_id)
        )
        conn.commit()
    return get(conn, sub_id)


def pause(conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int) -> bool:
    """Пауза всей подписки: и почта, и Telegram."""
    row = get_owned(conn, user, sub_id)
    if row is None:
        return False
    for leg in siblings(conn, user, row):
        if leg["status"] == "active":
            conn.execute("UPDATE alert_subscriptions SET status = 'paused' WHERE id = ?", (leg["id"],))
    conn.commit()
    return True


def resume(conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int, now: datetime) -> bool:
    row = get_owned(conn, user, sub_id)
    if row is None:
        return False
    for leg in siblings(conn, user, row):
        if leg["channel"] == "telegram" and not leg["tg_chat_id"]:
            continue  # ждёт привязки чата, включать нечего
        if leg["status"] in ("paused", "unsubscribed"):
            _reactivate(conn, leg, now)
    return True


def delete(conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int) -> bool:
    """Удалить подписку целиком, со всеми каналами."""
    row = get_owned(conn, user, sub_id)
    if row is None:
        return False
    conn.executemany("DELETE FROM alert_subscriptions WHERE id = ?", [(leg["id"],) for leg in siblings(conn, user, row)])
    conn.commit()
    return True


def set_frequency(conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int, frequency: str) -> bool:
    if frequency not in FREQUENCIES:
        raise AlertError("frequency", "Выберите, как часто присылать: сразу или раз в день.")
    row = get_owned(conn, user, sub_id)
    if row is None:
        return False
    conn.executemany(
        "UPDATE alert_subscriptions SET frequency = ? WHERE id = ?", [(frequency, leg["id"]) for leg in siblings(conn, user, row)]
    )
    conn.commit()
    return True


def add_telegram_to(
    conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int, now: datetime
) -> tuple[sqlite3.Row, str | None] | None:
    """Та же подписка в Telegram: новый поиск с теми же фильтрами и привязкой чата."""
    row = get_owned(conn, user, sub_id)
    if row is None:
        return None
    return subscribe_telegram(conn, user, filters_of(row), frequency=row["frequency"], now=now)


def add_email_to(
    conn: sqlite3.Connection, user: Mapping[str, Any], sub_id: int, now: datetime
) -> sqlite3.Row | None:
    """Та же подписка и на почту аккаунта: второй канал того же поиска (отключённый включается заново)."""
    row = get_owned(conn, user, sub_id)
    if row is None:
        return None
    mail, _ = subscribe_user(conn, user, filters_of(row), channel="email", frequency=row["frequency"], now=now)
    return mail


# ------------------------------------------------------------------ чистка и данные

def erase_email(conn: sqlite3.Connection, email: str) -> None:
    """Удалить гостевые подписки на почту (при удалении аккаунта с этой почтой)."""
    conn.execute(
        "DELETE FROM alert_subscriptions WHERE user_id IS NULL AND channel = 'email' AND email = ?",
        (accounts.normalize_email(email),),
    )
    conn.commit()


def purge(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """Убрать неподтверждённое, давно отключённое и старые журналы."""
    out = {}
    out["pending"] = conn.execute(
        "DELETE FROM alert_subscriptions WHERE status = 'pending' AND created_at < ?", (iso(now - PENDING_KEEP),)
    ).rowcount
    out["unsubscribed"] = conn.execute(
        "DELETE FROM alert_subscriptions WHERE user_id IS NULL AND status = 'unsubscribed' AND unsubscribed_at < ?",
        (iso(now - GUEST_UNSUB_KEEP),),
    ).rowcount
    out["log"] = conn.execute("DELETE FROM alert_log WHERE at < ?", (iso(now - LOG_KEEP),)).rowcount
    out["sent"] = conn.execute("DELETE FROM alert_sent WHERE sent_at < ?", (iso(now - SENT_KEEP),)).rowcount
    conn.execute("DELETE FROM channel_posts WHERE at < ?", (iso(now - POST_KEEP),))
    conn.execute("DELETE FROM channel_vacancies WHERE posted_at < ?", (iso(now - POST_KEEP),))
    conn.commit()
    return out


def stats(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Цифры для раздела «Система»."""
    day = iso(now - timedelta(hours=24))

    def one(sql: str, *args: Any) -> int:
        return int(conn.execute(sql, args).fetchone()[0] or 0)

    by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM alert_subscriptions GROUP BY status")}
    by_channel = {r[0]: r[1] for r in conn.execute(
        "SELECT channel, COUNT(*) FROM alert_subscriptions WHERE status = 'active' GROUP BY channel")}
    return {
        "total": sum(by_status.values()), "active": by_status.get("active", 0), "pending": by_status.get("pending", 0),
        "paused": by_status.get("paused", 0), "unsubscribed": by_status.get("unsubscribed", 0),
        "email": by_channel.get("email", 0), "telegram": by_channel.get("telegram", 0),
        "mail_sent_24h": one("SELECT COUNT(*) FROM alert_log WHERE channel='email' AND kind='digest' AND ok=1 AND at > ?", day),
        "mail_errors_24h": one("SELECT COUNT(*) FROM alert_log WHERE channel='email' AND ok=0 AND at > ?", day),
        "mail_sent_total": one("SELECT COUNT(*) FROM alert_log WHERE channel='email' AND kind='digest' AND ok=1"),
        "tg_sent_24h": one("SELECT COUNT(*) FROM alert_log WHERE channel='telegram' AND kind='digest' AND ok=1 AND at > ?", day),
        "tg_errors_24h": one("SELECT COUNT(*) FROM alert_log WHERE channel='telegram' AND ok=0 AND at > ?", day),
        "vacancies_sent_24h": one("SELECT COALESCE(SUM(vacancies), 0) FROM alert_log WHERE kind='digest' AND ok=1 AND at > ?", day),
        "posts_24h": one("SELECT COUNT(*) FROM channel_posts WHERE ok=1 AND at > ?", day),
        "post_errors_24h": one("SELECT COUNT(*) FROM channel_posts WHERE ok=0 AND at > ?", day),
        "last_error": (conn.execute(
            "SELECT error FROM alert_log WHERE ok=0 AND error != '' ORDER BY id DESC LIMIT 1").fetchone() or [""])[0],
    }
