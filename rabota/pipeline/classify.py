"""Классификация поста: vacancy, service, military, ad, other (см. POST_KINDS в rabota.models).

Порядок проверок важен: набор на службу и услуги мастеров отсеиваем раньше, заглушку без должности
считаем рекламой, а не вакансией.
"""
from __future__ import annotations

import re
from typing import Any

from rabota.models import POST_KINDS
from rabota.pipeline.contacts import extract_phones, extract_telegram
from rabota.pipeline.dictionaries import (
    AD_RE,
    HIRING_RE,
    LOAN_RE,
    MILITARY_RE,
    RESUME_RE,
    SEEKER_RE,
    SERVICE_RE,
    SERVICE_STRONG_RE,
    STUB_RE,
    VACANCY_STRUCTURE_RE,
    norm,
)
from rabota.pipeline.pay import parse_pay
from rabota.pipeline.titles import pick_profession

_PEOPLE_RE = re.compile(r"(?:нужн\w+|требу\w+|на\s+завтра|на\s+сегодня)\s+(?:\w+\s+){0,2}\d+\s+(?:человек|чел\b)|нужно\s+\d+\s+человек|\d+\s+человек")
_LABEL_RE = re.compile(r"(?m)^\W*(обязанности|требования|условия|график|зарплата|оплата|занятость|формат|опыт работы)\s*:")
_WORKERS_RE = re.compile(r"требу\w+\s+(?:срочно\s+)?(?:работник|рабоч|сотрудник|люд|мужчин|женщин|девуш)\w*\s+(?:на|в|для)\s+\w{4,}")
_STUB_LINES = re.compile(
    r"(?im)^\W*(?:запись\s+на\s+собеседование(?:\s+по\s+телефону)?|телефон|тел\.?|звоните|контакты?|по\s+всем\s+вопросам)\W*$"
)


def _strip_contacts(text: str) -> str:
    out = re.sub(r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", " ", text)
    out = re.sub(r"[+]?\d[\d\s()\-]{8,}\d", " ", out)
    return re.sub(r"@\w+", " ", out)


def is_empty_stub(text: str) -> bool:
    """Нет слов, кроме телефонов и фраз вроде "запись на собеседование"."""
    body = _STUB_LINES.sub("", _strip_contacts(text))
    body = re.sub(r"\bпо\s+телефону\b|\bзапись\s+на\s+собеседование\b", " ", norm(body))
    return not re.search(r"[a-zа-я]{3}", body)


def has_hiring_marker(text: str) -> bool:
    return bool(HIRING_RE.search(norm(text)))


def classify(text: str, extra: dict[str, Any] | None = None) -> str:
    """Вид поста по очищенному тексту. `extra` с полями источника (trudvsem, hh) считается вакансией."""
    kind = _classify(text, extra)
    assert kind in POST_KINDS
    return kind


def _classify(text: str, extra: dict[str, Any] | None) -> str:
    if not text or not text.strip() or is_empty_stub(text):
        return "ad"
    low = norm(text)
    structured = bool(extra) and any(k in extra for k in ("job-name", "job_name", "salary_min", "salary_max", "salary", "name", "title"))
    low_no_service = re.sub(r"воинск\w+\s+учет\w*|воинск\w+\s+обязанност\w*", " ", low)
    if MILITARY_RE.search(low_no_service):
        return "military"
    if SEEKER_RE.search(low):
        return "other"
    if LOAN_RE.search(low) and not HIRING_RE.search(low):
        return "ad"
    hiring_head = bool(HIRING_RE.search(norm("\n".join(text.split("\n")[:4]))))
    labels = len(set(_LABEL_RE.findall(low)))
    if RESUME_RE.search(low) and not HIRING_RE.search(low) and not VACANCY_STRUCTURE_RE.search(low) and labels < 2:
        return "other"
    formal = structured or labels >= 2
    if SERVICE_STRONG_RE.search(low + " ") and not hiring_head and not formal:
        return "service"
    hiring = bool(HIRING_RE.search(low))
    if SERVICE_RE.search(low) and not hiring and not formal and not VACANCY_STRUCTURE_RE.search(low):
        return "service"
    pay = parse_pay(text)
    phones = extract_phones(text)
    if AD_RE.search(low) and not pay.has_amount and not phones:
        return "ad"
    choice = pick_profession(text)
    people = bool(_PEOPLE_RE.search(low))
    structure = bool(VACANCY_STRUCTURE_RE.search(low))
    if structured:
        return "vacancy" if choice or structured else "other"
    if choice is None:
        if (hiring or people) and pay.has_amount and not STUB_RE.search(low):
            return "vacancy"
        if people and re.search(r"работ|подсобн|выгруз|разгруз|смен|объект", low):
            return "vacancy"
        if len(text) > 100 and _WORKERS_RE.search(low):
            return "vacancy"
        if hiring or STUB_RE.search(low) or extract_telegram(text):
            return "ad"
        return "other"
    if hiring or structure or people or pay.has_amount:
        return "vacancy"
    return "other"
