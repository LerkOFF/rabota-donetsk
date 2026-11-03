"""Автомодерация вакансий работодателей: стоп-лист, подозрительные формулировки, проверенный работодатель.

Решение принимает оценка `assess` (чистая функция от вакансии, стоп-листа и компании), применяет `apply`:

- `reject`: контакт или название работодателя в стоп-листе либо запрещённое содержание (закладки, дропперы, интим).
  Вакансия сразу отклонена с причиной, автор видит её в кабинете;
- `review`: есть подозрительные признаки (просьба заплатить, слишком высокая оплата, ссылки, крик заглавными,
  слово из стоп-листа). Вакансия идёт в очередь модерации с пометками, их видно администратору;
- `approve`: у проверенного работодателя (`companies.verified_at`) и без единой пометки вакансия публикуется сразу.

Остальные случаи: обычная очередь модерации без пометок.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rabota.admin import audit, stoplist
from rabota.admin.common import iso, now_utc
from rabota.employers import notify

REJECT = "reject"
REVIEW = "review"
APPROVE = "approve"
QUEUE = "queue"

FLAG_LABELS = {
    "stoplist_contact": "Контакт или работодатель в стоп-листе",
    "stoplist_keyword": "Слово из стоп-листа",
    "forbidden": "Запрещённое содержание",
    "money": "Просьба заплатить или внести деньги",
    "pay_high": "Слишком высокая оплата для вакансии",
    "links": "Ссылки или приглашение в мессенджер",
    "caps": "Текст заглавными буквами",
    "flood": "Много вакансий за сутки",
    "blocked_company": "Работодатель заблокирован",
}
# флаги, при которых вакансия отклоняется сама
REJECT_FLAGS = ("stoplist_contact", "forbidden", "blocked_company")

REJECT_TEXT = {
    "stoplist_contact": "Телефон, ник или название работодателя в списке недопустимых. Если это ошибка, напишите нам.",
    "forbidden": "В тексте вакансии запрещённое содержание: такие объявления на сайте не размещаются.",
    "blocked_company": "Аккаунт работодателя заблокирован. Если это ошибка, напишите нам.",
}

_FORBIDDEN = re.compile(
    r"закладчик|закладк[аиуе]\b|кладмен|дропп?ер|дроп[ыа]\b|обнал|наркот|интим[- ]?услуг|эскорт|досуг для взрослых",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"предоплат|залог\s+(за|\d)|внес(ти|ите|ение)\s+(\w+\s+)?(деньг|сумм|взнос|залог|\d)|вступительн\w+ взнос|взнос\b|"
    r"платн\w+ обучени|оплат\w+ обучени|обучение за (ваш )?счёт сотрудника|оплатить (комплект|форму|анкету|оформление|страховку)|"
    r"за оформлени|за трудоустройств|стартов\w+ (взнос|капитал|набор)|купить (комплект|набор|товар)|вложени",
    re.IGNORECASE,
)
_LINKS = re.compile(r"https?://|www\.|\bt\.me/|bit\.ly|tinyurl|wa\.me|vk\.cc|пиши(те)? (в )?(лс|личку|директ)", re.IGNORECASE)
# оплата, начиная с которой вакансия "слишком хорошая": в рублях за период
_PAY_HIGH = {"месяц": 250_000, "день": 15_000, "смена": 20_000, "час": 3_000, "задача": 60_000}
_FLOOD_PER_DAY = 6


@dataclass
class Assessment:
    decision: str = QUEUE
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    stop_entry_id: int | None = None

    def labels(self) -> list[str]:
        return [FLAG_LABELS.get(f, f) for f in self.flags]

    def reject_text(self) -> str:
        for flag in self.flags:
            if flag in REJECT_FLAGS:
                return REJECT_TEXT[flag]
        return ""


def _text(v: dict[str, Any]) -> str:
    return " ".join(str(v.get(k) or "") for k in ("profession", "title_raw", "summary", "requirements", "employer"))


def pay_is_high(v: dict[str, Any]) -> bool:
    top = max(int(v.get("pay_min") or 0), int(v.get("pay_max") or 0))
    limit = _PAY_HIGH.get(str(v.get("pay_period") or ""))
    return bool(top and limit and top >= limit)


def caps_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 40:
        return 0.0
    return sum(ch.isupper() for ch in letters) / len(letters)


def assess(
    vacancy: dict[str, Any], *, sl: stoplist.Stoplist | None = None, company: dict[str, Any] | None = None,
    recent_posts: int = 0,
) -> Assessment:
    """Оценка одной вакансии (словарь полей как в таблице vacancies). recent_posts: сколько вакансий автор
    разместил за последние сутки (текущая не считается)."""
    result = Assessment()
    flags = result.flags
    text = _text(vacancy)
    if company and company.get("blocked_at"):
        flags.append("blocked_company")
    if sl:
        match = sl.match(vacancy)
        if match is not None:
            result.stop_entry_id = match.entry_id
            if match.kind == "keyword":
                flags.append("stoplist_keyword")
                result.reasons.append(f"слово «{match.value}»")
            else:
                flags.append("stoplist_contact")
                result.reasons.append(f"{stoplist.KINDS[match.kind].lower()} {match.value}")
    if _FORBIDDEN.search(text):
        flags.append("forbidden")
    if _MONEY.search(text):
        flags.append("money")
    if pay_is_high(vacancy):
        flags.append("pay_high")
    if _LINKS.search(text):
        flags.append("links")
    if caps_ratio(str(vacancy.get("summary") or "")) > 0.6:
        flags.append("caps")
    if recent_posts >= _FLOOD_PER_DAY:
        flags.append("flood")
    if any(f in REJECT_FLAGS for f in flags):
        result.decision = REJECT
    elif flags:
        result.decision = REVIEW
    elif company and company.get("verified_at") and not company.get("blocked_at"):
        result.decision = APPROVE
    return result


def recent_posts(conn: sqlite3.Connection, owner_user_id: int | None, now: datetime, exclude_id: int = 0) -> int:
    if not owner_user_id:
        return 0
    since = (now.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE owner_user_id = ? AND id != ? AND created_at >= ?",
        (owner_user_id, exclude_id, since),
    ).fetchone()
    return int(row[0])


def load_flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def flag_labels(value: Any) -> list[str]:
    return [FLAG_LABELS.get(f, f) for f in load_flags(value)]


def apply(
    conn: sqlite3.Connection, vacancy_id: int, result: Assessment, now: datetime | None = None
) -> str:
    """Записать пометки и выполнить решение. Возвращает итог: reject, review, approve или queue."""
    now = now or now_utc()
    conn.execute("UPDATE vacancies SET automod_flags = ? WHERE id = ?",
                 (json.dumps(result.flags, ensure_ascii=False) if result.flags else None, vacancy_id))
    if result.decision == REJECT:
        note = result.reject_text()
        conn.execute(
            "UPDATE vacancies SET moderation_status = 'rejected', status = 'hidden', moderation_note = ? WHERE id = ?",
            (note, vacancy_id),
        )
        if result.stop_entry_id:
            stoplist.register_hits(conn, {result.stop_entry_id: 1}, now)
        audit.log(conn, None, "vacancy.automod_reject", "vacancy", vacancy_id, {"flags": result.flags}, now=now)
    elif result.decision == APPROVE:
        conn.execute(
            "UPDATE vacancies SET moderation_status = 'approved', status = 'active', moderation_note = NULL,"
            " last_seen_at = ?, published_at = ? WHERE id = ?",
            (iso(now), iso(now), vacancy_id),
        )
        audit.log(conn, None, "vacancy.automod_approve", "vacancy", vacancy_id, {"company_verified": True}, now=now)
    elif result.decision == REVIEW:
        audit.log(conn, None, "vacancy.automod_review", "vacancy", vacancy_id, {"flags": result.flags}, now=now)
    conn.commit()
    return result.decision


def moderate_new(
    conn: sqlite3.Connection, vacancy_id: int, now: datetime | None = None, *, premoderate: bool = True
) -> Assessment:
    """Оценить только что сохранённую вакансию работодателя и применить решение.

    premoderate=False (проверка отключена настройкой запуска) оставляет вакансию опубликованной, пометки пишутся."""
    now = now or now_utc()
    row = conn.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        return Assessment()
    vac = dict(row)
    vac["contacts"] = json.loads(vac.get("contacts") or "{}")
    company = None
    if vac.get("company_id"):
        c = conn.execute("SELECT * FROM companies WHERE id = ?", (vac["company_id"],)).fetchone()
        company = dict(c) if c else None
    result = assess(
        vac, sl=stoplist.Stoplist.load(conn), company=company,
        recent_posts=recent_posts(conn, vac.get("owner_user_id"), now, vacancy_id),
    )
    if not premoderate and result.decision in (QUEUE, REVIEW, APPROVE):
        conn.execute("UPDATE vacancies SET automod_flags = ? WHERE id = ?",
                     (json.dumps(result.flags, ensure_ascii=False) if result.flags else None, vacancy_id))
        conn.commit()
        return result
    apply(conn, vacancy_id, result, now)
    notify.moderation_pending(conn, vacancy_id, "new")  # только если вакансия осталась ждать решения
    return result
