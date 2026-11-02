"""Рассылка: найти новые вакансии под каждую подписку, не слать повторно, учесть лимиты.

Поиск использует те же условия, что и страница /vacancies (`queries.VISIBLE_SQL` и фильтры выдачи). Для подписки
берутся вакансии новее `baseline_id`, которых нет в `alert_sent`. Всё найденное помечается отправленным, в письмо
или сообщение идёт не больше десяти карточек, остальное уходит ссылкой «Все вакансии». Режимы:
сразу (не чаще раза в 10 минут) и раз в день (после 09:00 по Донецку).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from rabota.accounts.mailer import Mailer
from rabota.accounts.tokens import iso
from rabota.alerts import conf, filters, mailtext, service
from rabota.web import queries as q

log = logging.getLogger("rabota.alerts")

DONETSK = timezone(timedelta(hours=3))
DAILY_HOUR = 9
INSTANT_GAP = timedelta(minutes=10)
FETCH_CAP = 200          # столько вакансий помечается за один раз; остаток уйдёт со следующей подборкой
SUBS_PER_CYCLE = 500
FAILS_TO_PAUSE = 5       # подряд неудачных отправок по одной подписке, после чего она встаёт на паузу
CIRCUIT = 3              # подряд сбоев в одном цикле, после чего цикл прерывается (почтовый сервер лежит)


@dataclass
class Due:
    sub: sqlite3.Row
    rows: list[sqlite3.Row]
    total: int
    to: str  # почта или chat_id в виде строки


def find_new(conn: sqlite3.Connection, sub: sqlite3.Row, limit: int = FETCH_CAP) -> list[sqlite3.Row]:
    """Новые видимые вакансии под подписку, которые по ней ещё не отправлялись. Новые сверху."""
    f = filters.to_filters(service.filters_of(sub))
    where, args, _, _ = q._where(f)
    return conn.execute(  # type: ignore[no-any-return]
        f"SELECT * FROM vacancies WHERE {where} AND id > ? AND NOT EXISTS ("
        " SELECT 1 FROM alert_sent s WHERE s.subscription_id = ? AND s.vacancy_id = vacancies.id)"
        f" ORDER BY {q.ORDER_SQL} LIMIT ?",
        [*args, sub["baseline_id"], sub["id"], limit],
    ).fetchall()


def is_due(sub: sqlite3.Row, now: datetime) -> bool:
    """Пора ли смотреть подписку: «сразу» не чаще раза в 10 минут, «раз в день» после 09:00 по Донецку."""
    if sub["frequency"] == "instant":
        return not sub["last_sent_at"] or sub["last_sent_at"] <= iso(now - INSTANT_GAP)
    local = now.astimezone(DONETSK)
    slot = local.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    return not sub["last_checked_at"] or sub["last_checked_at"] < iso(slot.astimezone(now.tzinfo))


def email_of(conn: sqlite3.Connection, sub: sqlite3.Row) -> str | None:
    """Куда писать: почта аккаунта (если он активен и подтверждён) или подтверждённая почта гостя."""
    if sub["user_id"] is None:
        return sub["email"] or None
    row = conn.execute("SELECT email, status, email_verified_at FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
    if row is None or row["status"] != "active" or not row["email_verified_at"]:
        return None
    return str(row["email"])


def collect_due(conn: sqlite3.Connection, channel: str, now: datetime, limit: int = SUBS_PER_CYCLE) -> Iterator[Due]:
    subs = conn.execute(
        "SELECT * FROM alert_subscriptions WHERE status = 'active' AND channel = ? ORDER BY COALESCE(last_sent_at, '') LIMIT ?",
        (channel, limit),
    ).fetchall()
    for sub in subs:
        if not is_due(sub, now):
            continue
        if channel == "email":
            to = email_of(conn, sub)
        else:
            to = str(sub["tg_chat_id"]) if sub["tg_chat_id"] else None
        if not to:
            continue
        rows = find_new(conn, sub)
        if not rows:
            conn.execute("UPDATE alert_subscriptions SET last_checked_at = ? WHERE id = ?", (iso(now), sub["id"]))
            conn.commit()
            continue
        yield Due(sub, rows, len(rows), to)


def mark_sent(conn: sqlite3.Connection, due: Due, now: datetime, channel: str) -> None:
    stamp = iso(now)
    conn.executemany(
        "INSERT OR IGNORE INTO alert_sent(subscription_id, vacancy_id, sent_at) VALUES (?,?,?)",
        [(due.sub["id"], r["id"], stamp) for r in due.rows],
    )
    conn.execute(
        "UPDATE alert_subscriptions SET last_sent_at = ?, last_checked_at = ?, sent_count = sent_count + 1,"
        " error_count = 0, last_error = '' WHERE id = ?", (stamp, stamp, due.sub["id"]),
    )
    conn.commit()
    service.log_delivery(conn, now, channel=channel, kind="digest", subscription_id=due.sub["id"], vacancies=len(due.rows))


def mark_failure(conn: sqlite3.Connection, due: Due, error: str, now: datetime, channel: str) -> None:
    """Неудача: подписка остаётся, после нескольких подряд она встаёт на паузу, чтобы не биться в закрытый адрес."""
    conn.execute(
        "UPDATE alert_subscriptions SET error_count = error_count + 1, last_error = ? WHERE id = ?",
        (error[:200], due.sub["id"]),
    )
    conn.execute(
        "UPDATE alert_subscriptions SET status = 'paused' WHERE id = ? AND error_count >= ?", (due.sub["id"], FAILS_TO_PAUSE)
    )
    conn.commit()
    service.log_delivery(conn, now, channel=channel, kind="digest", ok=False, error=error, subscription_id=due.sub["id"])


def mails_last_hour(conn: sqlite3.Connection, now: datetime) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE channel = 'email' AND ok = 1 AND at > ?", (iso(now - timedelta(hours=1)),)
    ).fetchone()[0])


def run_email_cycle(
    conn: sqlite3.Connection, mailer: Mailer, now: datetime, *, site_url: str, secret: bytes,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Один проход рассылки писем. Ограничения: не больше N писем в час на сервер (настройка админки), пауза между
    письмами, остановка при серии сбоев отправки. Выключено в админке: ничего не отправляется и не помечается."""
    result: dict[str, Any] = {"sent": 0, "errors": 0, "limited": False, "disabled": False}
    if not conf.setting(conn, "alerts_email_enabled"):
        result["disabled"] = True
        return result
    budget = int(conf.setting(conn, "alerts_mail_per_hour")) - mails_last_hour(conn, now)
    pause = int(conf.setting(conn, "alerts_mail_pause"))
    streak = 0
    for due in collect_due(conn, "email", now):
        if budget <= 0:
            result["limited"] = True
            break
        mail = mailtext.digest_mail(due.sub, due.to, due.rows, due.total, site_url=site_url, secret=secret)
        try:
            mailer.send(mail)
        except Exception as exc:  # noqa: BLE001 - сбой одной почты не должен ронять рассылку
            mark_failure(conn, due, f"{type(exc).__name__}: {exc}", now, "email")
            result["errors"] += 1
            streak += 1
            if streak >= CIRCUIT:
                log.warning("рассылка писем остановлена: %d сбоев подряд", streak)
                break
            continue
        streak = 0
        mark_sent(conn, due, now, "email")
        result["sent"] += 1
        budget -= 1
        if pause:
            sleep(pause)
    return result
