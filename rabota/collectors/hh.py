"""Сборщик: hh.ru, разбор HTML поисковой выдачи.

Анонимный API api.hh.ru отвергает запросы, регистрация приложения недоступна, поэтому берём
обычную страницу поиска https://hh.ru/search/vacancy?area=2134&order_by=publication_time. Страница
отдаёт список вакансий встроенным JSON в теге <template id="HH-Lux-InitialState">. С параметром
enable_snippets=true в нём есть короткие фрагменты текста (требования, обязанности, условия).
Полный текст вакансии не запрашиваем, чтобы не нагружать сайт.

Ограничения:
- если hh.ru отдаёт капчу, проверку на робота, 403 или 429, капчу не обходим: fetch пишет
  предупреждение в лог и возвращает пустой список;
- если вёрстка сменилась и встроенного JSON нет, поведение то же;
- фрагменты текста есть не у всех вакансий, у части в text только название, условия и
  структурные поля (в extra).
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from rabota.config import USER_AGENT
from rabota.models import RawPost

NAME = "hh"
SOURCES = ("hh",)

SEARCH_URL = "https://hh.ru/search/vacancy"
TIMEOUT = 25.0
PAUSE_SECONDS = 2.0     # hh.ru чувствителен к частым запросам
MAX_PAGES = 5
MAX_ITEMS_ON_PAGE = 50

# области hh.ru
AREA_DNR = 2134
AREAS = {
    "ДНР": 2134,
    "Донецк": 118,
    "Макеевка": 2140,
    "Горловка": 2136,
    "Мариуполь": 2104,
    "Енакиево": 2138,
}
DEFAULT_AREAS = (AREA_DNR,)

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.DOTALL)
_ANTIBOT_MARKERS = ("captcha", "подтвердите, что вы не робот", "вы не робот", "ddos-guard", "доступ ограничен")

EXPERIENCE = {
    "noExperience": "не нужен",
    "between1And3": "от 1 до 3 лет",
    "between3And6": "от 3 до 6 лет",
    "moreThan6": "более 6 лет",
}
EMPLOYMENT = {
    "FULL": "полная занятость",
    "PART": "частичная занятость",
    "PROJECT": "проектная работа",
    "FLY_IN_FLY_OUT": "вахта",
    "VOLUNTEER": "волонтёрство",
    "PROBATION": "стажировка",
}
SCHEDULE_DAYS = {
    "FIVE_ON_TWO_OFF": "5/2",
    "SIX_ON_ONE_OFF": "6/1",
    "TWO_ON_TWO_OFF": "2/2",
    "THREE_ON_THREE_OFF": "3/3",
    "ONE_ON_TWO_OFF": "1/2",
    "ONE_ON_THREE_OFF": "1/3",
    "FOUR_ON_TWO_OFF": "4/2",
    "THREE_ON_TWO_OFF": "3/2",
    "TWO_ON_ONE_OFF": "2/1",
    "WEEKENDS": "по выходным",
    "FLEXIBLE": "свободный график",
}
WORK_FORMATS = {
    "ON_SITE": "на месте работодателя",
    "REMOTE": "удалённо",
    "HYBRID": "гибрид",
    "FIELD_WORK": "разъездной",
}
PAY_MODES = {"MONTH": "в месяц", "SHIFT": "за смену", "HOUR": "в час", "DAY": "в день",
             "WEEK": "в неделю", "FLY_IN_FLY_OUT": "за вахту", "SERVICE": "за услугу"}
SNIPPET_LABELS = (("resp", "Обязанности"), ("req", "Требования"), ("cond", "Условия"),
                  ("skill", "Навыки"), ("desc", "Описание"))
# тяжёлые служебные поля выдачи, в extra не нужны
_DROP_KEYS = {"vacancyProperties", "experimentalModes", "searchRid", "positionInRequest", "userLabels",
              "employerManager", "chatWritePossibility", "inboxPossibility", "notify", "canBeShared",
              "autoResponse", "acceptIncompleteResumes", "userTestPresent", "tags", "vacancyPlatforms",
              "extraLabels", "creationSiteId", "displayHost", "creationSite"}


class BlockedError(RuntimeError):
    """hh.ru не отдал список: анти-бот, капча или смена вёрстки."""


def _values(v: dict[str, Any], key: str) -> list[str]:
    """Значения вида [{"workFormatsElement": [...]}] в плоский список."""
    out: list[str] = []
    for block in v.get(key) or []:
        if isinstance(block, dict):
            out.extend(str(x) for x in block.get(key + "Element", []))
    return out


def extract_state(page: str) -> dict[str, Any]:
    """Встроенное состояние страницы. Бросает BlockedError, если его нет."""
    m = _STATE_RE.search(page)
    if not m:
        low = page.lower()
        if any(mark in low for mark in _ANTIBOT_MARKERS):
            raise BlockedError("hh.ru отдал страницу проверки на робота, капчу не обходим")
        raise BlockedError("в ответе hh.ru нет встроенного списка вакансий (изменилась вёрстка или анти-бот)")
    try:
        return json.loads(html.unescape(m.group(1)))
    except ValueError as exc:
        raise BlockedError(f"не разобран встроенный JSON hh.ru: {exc}") from exc


def parse_search(page: str) -> tuple[list[dict[str, Any]], int]:
    """Вакансии страницы и общее число найденного."""
    state = extract_state(page)
    result = state.get("vacancySearchResult")
    if not isinstance(result, dict):
        raise BlockedError("в состоянии страницы нет vacancySearchResult")
    return list(result.get("vacancies") or []), int(result.get("totalResults") or 0)


def _ts_iso(value: Any) -> str | None:
    if isinstance(value, dict):
        ts = value.get("@timestamp")
        if ts is not None:
            return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
        value = value.get("$")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(UTC).isoformat()
        except ValueError:
            return None
    return None


def _salary_text(v: dict[str, Any]) -> str:
    c = v.get("compensation") or {}
    lo, hi = c.get("from"), c.get("to")
    if not lo and not hi:
        return ""
    if lo and hi:
        s = f"от {lo} до {hi}"
    elif lo:
        s = f"от {lo}"
    else:
        s = f"до {hi}"
    cur = c.get("currencyCode", "RUR")
    s += " руб." if cur == "RUR" else f" {cur}"
    if c.get("mode") in PAY_MODES:
        s += " " + PAY_MODES[c["mode"]]
    if "gross" in c:
        s += ", до вычета налогов" if c["gross"] else ", на руки"
    return s


def build_text(v: dict[str, Any]) -> str:
    lines = [str(v.get("name") or "").strip()]

    def add(label: str, value: Any) -> None:
        if value:
            lines.append(f"{label}: {value}")

    company = v.get("company") or {}
    area = v.get("area") or {}
    address = v.get("address") or {}
    snippet = v.get("snippet") or {}
    add("Работодатель", company.get("visibleName") or company.get("name"))
    add("Зарплата", _salary_text(v))
    add("Город", area.get("name"))
    add("Адрес", address.get("displayName"))
    add("Район", (address.get("districtDto") or {}).get("name"))
    add("Опыт работы", EXPERIENCE.get(v.get("workExperience"), v.get("workExperience")))
    emp = v.get("employmentForm") or (v.get("employment") or {}).get("@type")
    add("Занятость", EMPLOYMENT.get(emp, emp))
    days = _values(v, "workScheduleByDays")
    add("График", ", ".join(SCHEDULE_DAYS.get(x, x) for x in days if x != "OTHER"))
    hours = [h.removeprefix("HOURS_") for h in _values(v, "workingHours") if h.startswith("HOURS_")]
    add("Часов в смене", ", ".join(hours))
    add("Формат работы", ", ".join(WORK_FORMATS.get(x, x) for x in _values(v, "workFormats")))
    for key, label in SNIPPET_LABELS:
        add(label, re.sub(r"\s+", " ", snippet.get(key) or "").strip())
    return "\n".join(lines)


def to_raw_post(v: dict[str, Any]) -> RawPost:
    vid = str(v["vacancyId"])
    extra = {k: val for k, val in v.items() if k not in _DROP_KEYS}
    return RawPost(
        source_type="hh",
        source="hh.ru",
        external_id=vid,
        url=f"https://hh.ru/vacancy/{vid}",
        text=build_text(v),
        published_at=_ts_iso(v.get("publicationTime")) or _ts_iso(v.get("creationTime")),
        image_urls=[],
        extra=extra,
    )


def _fetch_area(client: httpx.Client, area: int, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    per_page = min(max(limit, 10), MAX_ITEMS_ON_PAGE)
    for page_no in range(MAX_PAGES):
        params = {
            "area": area, "order_by": "publication_time", "items_on_page": per_page,
            "enable_snippets": "true", "search_field": "name", "text": "", "page": page_no,
        }
        resp = client.get(SEARCH_URL, params=params)
        if resp.status_code in (403, 429, 503):
            raise BlockedError(f"hh.ru ответил HTTP {resp.status_code}, вероятно анти-бот")
        if resp.status_code != 200:
            raise BlockedError(f"hh.ru ответил HTTP {resp.status_code}")
        vacancies, _total = parse_search(resp.text)
        items.extend(vacancies)
        if len(items) >= limit or len(vacancies) < per_page:
            break
        time.sleep(PAUSE_SECONDS)
    return items


def fetch(limit: int = 10, client: httpx.Client | None = None,
          areas: tuple[int, ...] = DEFAULT_AREAS) -> list[RawPost]:
    """До limit свежих вакансий по каждой области из areas (по умолчанию вся ДНР).

    При блокировке или сбое пишет предупреждение в лог и возвращает то, что успело собраться
    (чаще всего пустой список). Не бросает исключений.
    """
    if limit <= 0:
        return []
    own = client is None
    if client is None:
        client = httpx.Client(
            timeout=TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        )
    posts: dict[str, RawPost] = {}
    try:
        for i, area in enumerate(areas):
            if i:
                time.sleep(PAUSE_SECONDS)
            try:
                found = _fetch_area(client, area, limit)
            except BlockedError as exc:
                log.warning("hh: область %s пропущена: %s", area, exc)
                continue
            except httpx.HTTPError as exc:
                log.warning("hh: область %s, ошибка сети: %s", area, exc)
                continue
            for v in found[:limit]:
                if v.get("vacancyId") is not None:
                    posts.setdefault(str(v["vacancyId"]), to_raw_post(v))
    finally:
        if own:
            client.close()
    return sorted(posts.values(), key=lambda p: p.published_at or "", reverse=True)
