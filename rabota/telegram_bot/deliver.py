"""Доставка новых вакансий подписчикам Telegram. Те же правила, что у писем: найденное не повторяется, до десяти карточек."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from rabota.alerts import cards, conf, dispatch, filters, service
from rabota.telegram_bot.api import BotApi, BotApiError, url_button

log = logging.getLogger("rabota.telegram_bot")

BETWEEN_MESSAGES = 0.15  # пауза между сообщениями, лимит Telegram около 30 в секунду на бота


def message_for(due: dispatch.Due, site_url: str) -> tuple[str, dict[str, Any]]:
    data = service.filters_of(due.sub)
    items = cards.cards(due.rows, site_url)
    header = f"{cards.count_phrase(due.total).capitalize()}: {due.sub['title']}"
    more = due.total - len(items)
    footer = (f"И ещё {more} на сайте.\n" if more > 0 else "") + "Отключить подписки: /stop"
    return (
        cards.telegram_list(items, header=header, footer=footer),
        url_button("Все вакансии", filters.vacancies_url(site_url, data)),
    )


async def run_telegram_cycle(
    conn: sqlite3.Connection, api: BotApi, now: datetime, *, site_url: str,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    result: dict[str, Any] = {"sent": 0, "errors": 0, "disabled": False, "stopped": 0}
    if not conf.setting(conn, "tg_bot_enabled"):
        result["disabled"] = True
        return result
    streak = 0
    for due in dispatch.collect_due(conn, "telegram", now):
        text, markup = message_for(due, site_url)
        try:
            await api.send_message(int(due.to), text, reply_markup=markup)
        except BotApiError as exc:
            if exc.code == 429:
                log.warning("Telegram просит подождать %s с, доставка прервана", exc.retry_after)
                dispatch.mark_failure(conn, due, f"лимит Telegram: {exc.retry_after} с", now, "telegram")
                result["errors"] += 1
                break
            if exc.chat_gone:
                service.stop_chat(conn, int(due.to), now, f"чат недоступен: {exc.description}")
                service.log_delivery(conn, now, channel="telegram", kind="digest", ok=False,
                                     error=f"чат недоступен: {exc.description}", subscription_id=due.sub["id"])
                result["stopped"] += 1
                continue
            dispatch.mark_failure(conn, due, str(exc), now, "telegram")
            result["errors"] += 1
            streak += 1
            if streak >= dispatch.CIRCUIT:
                break
            continue
        streak = 0
        dispatch.mark_sent(conn, due, now, "telegram")
        result["sent"] += 1
        await sleep(BETWEEN_MESSAGES)
    return result
