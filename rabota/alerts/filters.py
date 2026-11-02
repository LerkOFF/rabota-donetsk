"""Фильтры подписки: проверка, нормализация, человекочитаемое название и ссылка на выдачу.

Подписка хранит те же параметры, что и страница /vacancies (q, city, district, category, work_format, pay_period,
no_experience, side_job, daily, adult, direct, pay_from). Сортировка и страница в подписку не входят.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rabota.web import format as fmt
from rabota.web import queries as q

FILTER_KEYS = (
    "q", "city", "district", "category", "work_format", "pay_period",
    "no_experience", "side_job", "daily", "adult", "direct", "pay_from",
)
BOOL_KEYS = ("no_experience", "side_job", "daily", "adult", "direct")
TITLE_MAX = 120

# города ДНР в предложном падеже: "в Донецке"
_IN_CITY = {
    "донецк": "Донецке", "макеевка": "Макеевке", "горловка": "Горловке", "мариуполь": "Мариуполе",
    "енакиево": "Енакиеве", "харцызск": "Харцызске", "ясиноватая": "Ясиноватой", "торез": "Торезе",
    "снежное": "Снежном", "шахтерск": "Шахтерске", "шахтёрск": "Шахтёрске", "амвросиевка": "Амвросиевке",
    "докучаевск": "Докучаевске", "новоазовск": "Новоазовске", "тельманово": "Тельманове", "кировское": "Кировском",
    "зугрэс": "Зугрэсе", "иловайск": "Иловайске", "комсомольское": "Комсомольском", "волноваха": "Волновахе",
}


def in_city(city: str) -> str:
    """"в Донецке". Незнакомое название склоняется по простому правилу, а если не получается, пишется как есть."""
    name = " ".join((city or "").split())
    if not name:
        return ""
    known = _IN_CITY.get(name.lower())
    if known:
        return f"в {known}"
    low = name.lower()
    if low.endswith(("ск", "нск", "ль", "ец")):
        return f"в {name}е" if not low.endswith("ь") else f"в {name[:-1]}е"
    if low.endswith("а"):
        return f"в {name[:-1]}е"
    return f"в городе {name}"


def to_filters(data: Mapping[str, Any]) -> q.Filters:
    """Словарь подписки или параметры формы -> Filters (лишние ключи отбрасываются, значения чистятся)."""
    flat = {k: ("1" if v is True else "" if v in (False, None) else str(v)) for k, v in data.items() if k in FILTER_KEYS}
    return q.Filters.from_params(flat)


def to_dict(f: q.Filters) -> dict[str, Any]:
    """Только заданные значения: пустые и выключенные не хранятся."""
    out: dict[str, Any] = {}
    for key in FILTER_KEYS:
        value = getattr(f, key)
        if value:
            out[key] = True if key in BOOL_KEYS else (q.fold(value) if key == "q" else value)  # поиск без учёта регистра
    return out


def normalize(data: Mapping[str, Any]) -> dict[str, Any]:
    return to_dict(to_filters(data))


def has_conditions(data: Mapping[str, Any]) -> bool:
    """Подписка на «всё подряд» не нужна: нужен поиск или хотя бы один фильтр."""
    return bool(normalize(data))


def key_of(data: Mapping[str, Any]) -> str:
    return json.dumps(normalize(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def describe(data: Mapping[str, Any]) -> str:
    """Название подписки: "бариста в Донецке", "подработка, от 30 000 ₽ в Донецке"."""
    d = normalize(data)
    extras: list[str] = []
    core = (d.get("q") or "").strip().lower()
    if not core and d.get("category"):
        core = fmt.CATEGORY_LABELS.get(d["category"], d["category"]).lower()
    elif d.get("category"):
        extras.append(fmt.CATEGORY_LABELS.get(d["category"], d["category"]).lower())
    if d.get("side_job"):
        extras.append("подработка")
    if d.get("daily"):
        extras.append("выплата каждый день")
    if d.get("no_experience"):
        extras.append("без опыта")
    if d.get("adult"):
        extras.append("с 18 лет")
    if d.get("direct"):
        extras.append("от работодателей")
    if d.get("work_format"):
        extras.append(fmt.WORK_FORMAT_LABELS.get(d["work_format"], d["work_format"]).lower())
    if d.get("pay_from"):
        extras.append(f"от {fmt.money(int(d['pay_from']))} ₽")
    if d.get("pay_period") and not d.get("pay_from"):
        extras.append(f"оплата: {d['pay_period']}")
    if not core and extras:
        core, extras = extras[0], extras[1:]
    core = core or "вакансии"
    if d.get("district"):
        place = f"в районе {d['district']}"
        if d.get("city"):
            place += f", {d['city']}"
    elif d.get("city"):
        place = in_city(d["city"])
    else:
        place = "в Донецке и ДНР"
    text = core + (", " + ", ".join(extras) if extras else "") + " " + place
    return " ".join(text.split())[:TITLE_MAX]


def query_string(data: Mapping[str, Any]) -> str:
    return to_filters(data).query_string()


def vacancies_url(site_url: str, data: Mapping[str, Any]) -> str:
    qs = query_string(data)
    return f"{site_url}/vacancies" + (f"?{qs}" if qs else "")
