"""Автопостинг в канал сайта по рубрикам: подработка на сегодня, топ зарплат, новые вакансии часа.

Правила: не больше N постов в час (настройка админки), ночью (с 22:00 до 08:00 по Донецку) тишина, одна вакансия
попадает в канал один раз во всех рубриках, дневные рубрики выходят раз в сутки, «новые вакансии часа» раз в час.
За один проход публикуется не больше одного поста. После сбоя новая попытка не раньше чем через 10 минут.
Закреплённые и прочие посты владельца бот не трогает: он только добавляет новые. Без TG_CHANNEL_ID ничего не делает.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from rabota.accounts.tokens import iso
from rabota.alerts import cards, conf
from rabota.telegram_bot.api import BotApi, BotApiError, url_button
from rabota.web import queries as q

DONETSK = timezone(timedelta(hours=3))
QUIET_FROM, QUIET_TO = 22, 8
RETRY_AFTER_FAIL = timedelta(minutes=10)
UNSEEN = "NOT EXISTS (SELECT 1 FROM channel_vacancies c WHERE c.vacancy_id = vacancies.id)"
HASHTAGS = "#вакансии #Донецк #работа"


@dataclass
class Post:
    rubric: str
    key: str
    text: str
    rows: list[sqlite3.Row]
    button_text: str
    button_url: str


def _fresh_since(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _select(conn: sqlite3.Connection, extra: str, args: list[Any], order: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(  # type: ignore[no-any-return]
        f"SELECT * FROM vacancies WHERE {q.VISIBLE_SQL} AND {UNSEEN} AND {extra} ORDER BY {order} LIMIT ?", [*args, limit],
    ).fetchall()


def _text(header: str, lead: str, rows: list[sqlite3.Row], site_url: str, link: str, link_label: str) -> str:
    items = cards.cards(rows, site_url)
    body = cards.telegram_list(items, header=header)
    head, _, rest = body.partition("\n\n")
    parts = [head, lead, rest, f'{link_label}: <a href="{link}">{link.split("//", 1)[-1]}</a>', HASHTAGS]
    return "\n\n".join(p for p in parts if p)


def plan_next(conn: sqlite3.Connection, now: datetime, site_url: str) -> Post | None:
    """Следующий пост или None, если сейчас публиковать не нужно. Ничего не пишет в базу."""
    if not conf.setting(conn, "channel_enabled"):
        return None
    local = now.astimezone(DONETSK)
    if local.hour >= QUIET_FROM or local.hour < QUIET_TO:
        return None
    if conn.execute("SELECT COUNT(*) FROM channel_posts WHERE ok = 0 AND at > ?", (iso(now - RETRY_AFTER_FAIL),)).fetchone()[0]:
        return None
    per_hour = int(conf.setting(conn, "channel_posts_per_hour"))
    if conn.execute(
        "SELECT COUNT(*) FROM channel_posts WHERE ok = 1 AND at > ?", (iso(now - timedelta(hours=1)),)
    ).fetchone()[0] >= per_hour:
        return None

    def done(key: str) -> bool:
        return bool(conn.execute("SELECT 1 FROM channel_posts WHERE post_key = ? AND ok = 1", (key,)).fetchone())

    day = local.strftime("%Y-%m-%d")
    if QUIET_TO <= local.hour < 12 and not done(f"side_today:{day}"):
        rows = _select(
            conn, "employment IN ('на сегодня', 'подработка') AND datetime(created_at) >= datetime(?)",
            [_fresh_since(now, 20)], "CASE employment WHEN 'на сегодня' THEN 0 ELSE 1 END, " + q.ORDER_SQL, 7,
        )
        if len(rows) >= 3:
            return Post(
                "side_today", f"side_today:{day}",
                _text("Подработка на сегодня в Донецке", "Свежие предложения, с которых можно начать уже сегодня.", rows,
                      site_url, f"{site_url}/vacancies?side_job=1", "Вся подработка"),
                rows, "Вся подработка", f"{site_url}/vacancies?side_job=1",
            )
    if 12 <= local.hour < 21 and not done(f"top_pay:{day}"):
        rows = _select(conn, f"{q.PAY_SQL} IS NOT NULL AND datetime(created_at) >= datetime(?)", [_fresh_since(now, 24 * 7)],
                       f"{q.PAY_SQL} DESC, {q.ORDER_SQL}", 5)
        if len(rows) >= 3:
            return Post(
                "top_pay", f"top_pay:{day}",
                _text("Топ зарплат недели в Донецке", "Самая высокая оплата среди новых вакансий. Условия уточняйте у работодателя.",
                      rows, site_url, f"{site_url}/vacancies?sort=pay", "Вакансии по оплате"),
                rows, "Вакансии по оплате", f"{site_url}/vacancies?sort=pay",
            )
    hour_key = f"new_hour:{day}T{local.hour:02d}"
    if not done(hour_key):
        rows = _select(conn, "datetime(created_at) >= datetime(?)", [_fresh_since(now, 4)], q.ORDER_SQL, 5)
        if len(rows) >= 2:
            return Post(
                "new_hour", hour_key,
                _text("Новые вакансии часа в Донецке", "Только что появились на сайте.", rows, site_url,
                      f"{site_url}/vacancies", "Все вакансии"),
                rows, "Все вакансии", f"{site_url}/vacancies",
            )
    return None


def record(conn: sqlite3.Connection, post: Post, now: datetime, ok: bool, error: str = "", message_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO channel_posts(at, rubric, post_key, vacancy_ids, ok, error, message_id) VALUES (?,?,?,?,?,?,?)",
        (iso(now), post.rubric, post.key, json.dumps([r["id"] for r in post.rows]), 1 if ok else 0, error[:200], message_id),
    )
    if ok:
        conn.executemany(
            "INSERT OR IGNORE INTO channel_vacancies(rubric, vacancy_id, posted_at) VALUES (?,?,?)",
            [(post.rubric, r["id"], iso(now)) for r in post.rows],
        )
    conn.commit()


async def run_channel_cycle(
    conn: sqlite3.Connection, api: BotApi, channel_id: str, now: datetime, *, site_url: str,
) -> dict[str, Any]:
    """Опубликовать следующий пост, если пора. Возвращает {"posted": рубрика или "", "error": текст}."""
    result: dict[str, Any] = {"posted": "", "error": ""}
    post = plan_next(conn, now, site_url)
    if post is None:
        return result
    try:
        message_id = await api.send_message(channel_id, post.text, reply_markup=url_button(post.button_text, post.button_url))
    except BotApiError as exc:
        record(conn, post, now, False, str(exc))
        result["error"] = str(exc)
        return result
    record(conn, post, now, True, message_id=message_id)
    result["posted"] = post.rubric
    return result
