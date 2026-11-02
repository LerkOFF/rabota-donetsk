"""Правила резюме: пределы, словари, очистка и проверка анкеты, заполненность. Без базы и без веба."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from rabota import legal
from rabota.messaging.rules import clean_text, count_links
from rabota.models import EMPLOYMENTS, PAY_PERIODS, WORK_FORMATS
from rabota.pipeline.dictionaries import LEMMA_CATEGORY
from rabota.web import format as fmt

MAX_RESUMES = 3  # резюме на одного человека
MAX_ABOUT = 500
MAX_SKILLS = 10
MAX_SKILL_LEN = 30
MAX_PLACES = 3
MAX_PROFESSION = 80
MIN_PROFESSION = 3
MAX_EXPERIENCE_YEARS = 60
MAX_PAY = 5_000_000
STALE_DAYS = 365  # резюме, которое не обновляли дольше, работодателям больше не показывается
MAX_INVITE_TEXT = 1000
MAX_CONTACTS_PER_DAY = 30  # сколько раз работодатель может открыть контакты за сутки
MAX_INVITES_PER_DAY = 20
PER_PAGE = 20

VISIBILITIES = ("public", "open", "hidden")
VISIBILITY_LABELS = {
    "public": "Всем работодателям",
    "open": "Только по отклику",
    "hidden": "Скрыто",
}
VISIBILITY_HINTS = {
    "public": "Резюме найдут в поиске работодатели, которые зарегистрированы на сайте. Контакты они увидят только по кнопке.",
    "open": "В поиске вас нет. Работодатель увидит резюме, только если вы откликнулись с ним на его вакансию.",
    "hidden": "Резюме никому не видно. Его можно вернуть в любой момент.",
}
EXPERIENCE_FILTERS = {"": "Любой", "0": "Без опыта", "1": "От 1 года", "3": "От 3 лет", "5": "От 5 лет"}
PAY_PERIOD_LABELS = {"месяц": "в месяц", "смена": "за смену", "день": "в день", "час": "в час", "задача": "за задачу"}
WORK_FORMAT_LABELS = {
    "офис": "Офис", "точка": "Точка или зал", "объект": "Объект", "склад": "Склад", "выезд": "Разъездная работа",
    "удалённо": "Удалённо", "вахта": "Вахта",
}
EMPLOYMENT_LABELS = {"постоянно": "Постоянная", "подработка": "Подработка", "на сегодня": "На сегодня", "сменная": "Сменная"}

CONSENT_SLUG = "resume"
PROFESSION_SUGGESTIONS = tuple(sorted(LEMMA_CATEGORY))

_TG = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_TAG_SPLIT = re.compile(r"[,;\n]+")
_SKILL_OK = re.compile(r"^[\w .+#/&()-]+$", re.UNICODE)


def consent_version() -> str:
    """Редакция текста согласия на публикацию резюме, как она показана на странице документа."""
    stamp = (os.environ.get("LEGAL_UPDATED_AT") or "").strip() or legal.UPDATED_AT
    return f"{CONSENT_SLUG} {stamp}"


def ucfirst(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def short_name(full: str | None) -> str:
    """"Мария Петрова" -> "Мария П.": пока работодатель не открыл контакты, фамилия не показывается."""
    parts = (full or "").split()
    if not parts:
        return "Соискатель"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][:1].upper()}."


def loads(value: str | None, default: Any) -> Any:
    try:
        data = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _line(value: Any, limit: int) -> str:
    return " ".join(clean_text(str(value or "")).split())[:limit]


def parse_skills(raw: str) -> list[str]:
    """Навыки из строки: через запятую, точку с запятой или с новой строки. Повторы и пустые убираются."""
    seen: set[str] = set()
    out: list[str] = []
    for part in _TAG_SPLIT.split(raw or ""):
        tag = _line(part, 200)
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def _digits(value: str, limit: int = 9) -> int | None:
    raw = "".join(ch for ch in value if ch.isdigit())[:limit]
    return int(raw) if raw else None


def validate(form: dict[str, Any], *, consented_now: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    """Проверка анкеты. Возвращает очищенные значения (в том числе для повторного показа формы) и ошибки по полям.

    consented_now: у резюме уже есть действующее согласие этой редакции (галочка всё равно нужна при каждом сохранении
    открытого резюме, но её отсутствие объясняется иначе).
    """
    errors: dict[str, str] = {}
    getlist = form.getlist if hasattr(form, "getlist") else None

    def many(name: str) -> list[str]:
        if getlist:
            return list(getlist(name))
        value = form.get(name)
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)] if value else []

    def one(name: str) -> str:
        return str(form.get(name) or "")

    profession = _line(one("profession"), MAX_PROFESSION)
    if len(profession) < MIN_PROFESSION:
        errors["profession"] = f"Укажите профессию: хотя бы {MIN_PROFESSION} буквы."
    elif not re.search(r"[A-Za-zА-Яа-яЁё]", profession) or count_links(profession):
        errors["profession"] = "Профессия: только слова, без ссылок и контактов."

    city = _line(one("city"), 60) or "Донецк"
    district = _line(one("district"), 60)
    for name, value in (("city", city), ("district", district)):
        if count_links(value):
            errors[name] = "Ссылки здесь не нужны."

    pay_min = _digits(one("pay_min"))
    pay_period = _line(one("pay_period"), 20)
    if pay_min is not None and not 1 <= pay_min <= MAX_PAY:
        errors["pay_min"] = f"Зарплата: число от 1 до {MAX_PAY:,}.".replace(",", " ")
        pay_min = None
    if pay_period and pay_period not in PAY_PERIODS:
        errors["pay_period"] = "Выберите период из списка."
    if pay_min and not pay_period and "pay_period" not in errors:
        errors["pay_period"] = "Укажите, за что хотите получать эту сумму: за месяц, смену, день или час."
    if not pay_min:
        pay_period = pay_period if pay_period in PAY_PERIODS else ""

    formats = [f for f in WORK_FORMATS if f in many("work_formats")]
    employments = [e for e in EMPLOYMENTS if e in many("employments")]
    schedule = _line(one("schedule"), 60)
    if count_links(schedule):
        errors["schedule"] = "Ссылки здесь не нужны."

    years_raw = one("experience_years").strip()
    years: int | None = None
    if years_raw:
        years = _digits(years_raw, 3)
        if years is None or years > MAX_EXPERIENCE_YEARS:
            errors["experience_years"] = f"Стаж: число лет от 0 до {MAX_EXPERIENCE_YEARS}."
            years = None

    places: list[dict[str, str]] = []
    for i in range(1, MAX_PLACES + 1):
        place = {
            "place": _line(one(f"place_{i}"), 80), "role": _line(one(f"role_{i}"), 80),
            "period": _line(one(f"period_{i}"), 40),
        }
        if not any(place.values()):
            continue
        if any(count_links(v) for v in place.values()):
            errors[f"place_{i}"] = "Ссылки в местах работы не допускаются."
        elif not place["place"] and not place["role"]:
            errors[f"place_{i}"] = "Укажите место работы или должность."
        places.append(place)

    skills = parse_skills(one("skills"))
    if len(skills) > MAX_SKILLS:
        errors["skills"] = f"Навыков не больше {MAX_SKILLS}, сейчас {len(skills)}."
        skills = skills[:MAX_SKILLS]
    elif any(len(s) > MAX_SKILL_LEN for s in skills):
        errors["skills"] = f"Один навык не длиннее {MAX_SKILL_LEN} знаков."
    elif any(not _SKILL_OK.match(s) or count_links(s) for s in skills):
        errors["skills"] = "Навыки: слова без ссылок и необычных знаков."

    about = clean_text(one("about"))
    if len(about) > MAX_ABOUT:
        errors["about"] = f"О себе не больше {MAX_ABOUT} знаков, сейчас {len(about)}."
    elif count_links(about):
        errors["about"] = "В резюме нельзя оставлять ссылки: для связи есть поля телефона и Telegram."

    phone = ""
    if _line(one("contact_phone"), 40):
        norm = fmt.normalize_phone(one("contact_phone"))
        if norm is None:
            errors["contact_phone"] = "Телефон в формате +7 949 123-45-67."
        else:
            phone = norm
    telegram = _line(one("contact_telegram"), 64).removeprefix("https://t.me/").removeprefix("@")
    if telegram and not _TG.match(telegram):
        errors["contact_telegram"] = "Ник Telegram вида @name: латиница, цифры, подчёркивание, от 4 знаков."
        telegram = ""

    visibility = one("visibility") or "hidden"
    if visibility not in VISIBILITIES:
        errors["visibility"] = "Выберите, кому показывать резюме."
        visibility = "hidden"
    consent = one("consent") == "1"
    if visibility != "hidden" and not consent:
        errors["consent"] = (
            "Нужно согласие на передачу резюме работодателям. Без него резюме можно только скрыть."
            if not consented_now else "Подтвердите согласие: оно нужно при каждом сохранении открытого резюме."
        )

    clean: dict[str, Any] = {
        "profession": profession, "city": city, "district": district, "relocate": one("relocate") == "1",
        "remote": one("remote") == "1", "pay_min": pay_min, "pay_period": pay_period, "work_formats": formats,
        "employments": employments, "schedule": schedule, "experience_years": years, "places": places,
        "skills": skills, "about": about, "contact_phone": phone, "contact_telegram": telegram,
        "visibility": visibility, "consent": consent,
    }
    return clean, errors


# ------------------------------------------------------------------ заполненность

def completeness(resume: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """Процент заполненности и подсказки, что добавить: (очки, текст), самое выгодное первым."""
    checks: list[tuple[bool, int, str]] = [
        (bool(resume.get("profession")), 15, "Укажите профессию"),
        (bool(resume.get("pay_min")), 10, "Добавьте желаемую зарплату: работодатели чаще пишут тем, у кого она указана"),
        (bool(resume.get("district")), 5, "Укажите район, где вам удобно работать"),
        (bool(resume.get("work_formats")) or bool(resume.get("employments")),
         10, "Отметьте формат и вид занятости: офис, смены, подработка"),
        (resume.get("experience_years") is not None, 10, "Укажите стаж, даже если опыта нет (напишите 0)"),
        (bool(resume.get("places")), 15, "Добавьте последнее место работы, коротко: где и кем"),
        (len(resume.get("skills") or []) >= 3, 15, "Добавьте хотя бы три навыка"),
        (len(resume.get("about") or "") >= 80, 10, "Расскажите о себе в паре предложений"),
        (bool(resume.get("contact_phone") or resume.get("contact_telegram")),
         10, "Оставьте телефон или Telegram, чтобы работодатель мог связаться"),
    ]
    score = sum(points for ok, points, _ in checks if ok)
    tips = [{"points": points, "text": text} for ok, points, text in checks if not ok]
    tips.sort(key=lambda t: -t["points"])
    return min(score, 100), tips
