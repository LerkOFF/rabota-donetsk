"""Карточки вакансий для писем, сообщений бота и постов канала: одни и те же данные, разная вёрстка."""
from __future__ import annotations

import html
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from rabota.web import format as fmt

MAX_CARDS = 10
TELEGRAM_LIMIT = 4000


def card(row: sqlite3.Row | Mapping[str, Any], site_url: str) -> dict[str, str]:
    """Вакансия для показа: заголовок, оплата, место, работодатель и ссылка на сайт."""
    title = " ".join(str(row["profession"] or "Вакансия").split())
    title = title[:1].upper() + title[1:]
    pay = fmt.pay_parts(row["pay_min"], row["pay_max"], row["pay_period"])
    employer = " ".join(str(row["employer"] or "").split())[:60]
    return {
        "id": str(row["id"]),
        "title": title,
        "pay": pay["text"] if pay["known"] else "",
        "place": fmt.location_label(row["city"], row["district"]),
        "employer": employer,
        "url": f"{site_url}/vacancy/{row['id']}",
        "details": " · ".join(p for p in (pay["text"] if pay["known"] else "", fmt.location_label(row["city"], row["district"]), employer) if p),
    }


def cards(rows: Sequence[sqlite3.Row | Mapping[str, Any]], site_url: str, limit: int = MAX_CARDS) -> list[dict[str, str]]:
    return [card(r, site_url) for r in rows[:limit]]


def count_phrase(n: int) -> str:
    return fmt.count_label(n, "новая вакансия", "новые вакансии", "новых вакансий")


def telegram_list(items: Sequence[dict[str, str]], *, header: str, footer: str = "") -> str:
    """Текст сообщения в HTML для Telegram. Все данные экранируются, длина не выходит за лимит."""
    lines = [f"<b>{html.escape(header)}</b>", ""]
    for i, c in enumerate(items, 1):
        lines.append(f'{i}. <a href="{html.escape(c["url"], quote=True)}">{html.escape(c["title"])}</a>')
        if c["details"]:
            lines.append(f"    {html.escape(c['details'])}")
    if footer:
        lines += ["", footer]
    text = "\n".join(lines)
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 1].rsplit("\n", 1)[0]
    return text
