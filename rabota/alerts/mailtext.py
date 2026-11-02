"""Письма подписок: подтверждение почты и подборка новых вакансий (текст и HTML, заголовки List-Unsubscribe)."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from rabota.accounts.mailer import EMAIL_TEMPLATES_DIR, SITE_NAME, Mail
from rabota.alerts import cards as cards_mod
from rabota.alerts import filters, service

_txt = Environment(
    loader=FileSystemLoader(str(EMAIL_TEMPLATES_DIR)), autoescape=False, undefined=StrictUndefined,
    keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
)
_html = Environment(
    loader=FileSystemLoader(str(EMAIL_TEMPLATES_DIR)), autoescape=True, undefined=StrictUndefined,
    keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
)


def unsubscribe_url(site_url: str, sub_id: int, secret: bytes) -> str:
    return f"{site_url}/alerts/unsubscribe/{service.make_token(sub_id, 'unsub', secret)}"


def subject_for(title: str) -> str:
    return f"Новые вакансии: {title}"[:150]


def digest_mail(
    sub: sqlite3.Row | Mapping[str, Any], to: str, rows: Sequence[sqlite3.Row], total: int, *, site_url: str, secret: bytes,
) -> Mail:
    """Подборка: до десяти карточек, кнопка «Все вакансии», отписка. total: сколько всего нашлось новых."""
    data = service.filters_of(sub)
    items = cards_mod.cards(rows, site_url)
    unsub = unsubscribe_url(site_url, sub["id"], secret)
    ctx = {
        "site_name": SITE_NAME, "site_url": site_url, "title": sub["title"], "items": items, "total": total,
        "more": max(0, total - len(items)), "all_url": filters.vacancies_url(site_url, data),
        "manage_url": f"{site_url}/alerts", "unsubscribe_url": unsub, "count_text": cards_mod.count_phrase(total),
        "frequency_text": service.FREQUENCY_LABELS.get(sub["frequency"], ""),
    }
    return Mail(
        to=to, subject=subject_for(sub["title"]), body=_txt.get_template("alerts_digest.txt").render(**ctx),
        html=_html.get_template("alerts_digest.html").render(**ctx),
        headers=(
            ("List-Unsubscribe", f"<{unsub}>"),
            ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
            ("Precedence", "bulk"),
        ),
    )


def confirm_mail(sub: sqlite3.Row | Mapping[str, Any], code: str, *, site_url: str, secret: bytes) -> Mail:
    ctx = {
        "site_name": SITE_NAME, "site_url": site_url, "title": sub["title"], "code": code, "ttl_minutes": 30,
        "link": f"{site_url}/alerts/confirm/{service.make_token(sub['id'], 'confirm', secret)}",
        "frequency_text": service.FREQUENCY_LABELS.get(sub["frequency"], ""),
    }
    return Mail(
        to=sub["email"], subject=f"Код {code}: подтвердите подписку на вакансии",
        body=_txt.get_template("alerts_confirm.txt").render(**ctx),
        html=_html.get_template("alerts_confirm.html").render(**ctx),
    )
