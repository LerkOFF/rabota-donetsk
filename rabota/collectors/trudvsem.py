"""Сборщик: Работа России (trudvsem.ru), открытый API.

Адрес: https://opendata.trudvsem.ru/api/v1/vacancies/region/9300000000000
Код региона ДНР 9300000000000. API отдаёт структурные поля, их целиком кладём в
RawPost.extra, а в RawPost.text собираем связный текст для разбора.
Ответ API уже идёт от свежих к старым по date_modify, но порядок проверяем сами.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from rabota.config import USER_AGENT
from rabota.models import RawPost

NAME = "trudvsem"
SOURCES = ("trudvsem",)

API_URL = "https://opendata.trudvsem.ru/api/v1/vacancies/region/{region}"
REGION_DNR = "9300000000000"
TIMEOUT = 25.0
PAUSE_SECONDS = 1.0     # вежливая пауза между запросами
MAX_PAGE = 100          # больше API за раз не отдаёт
RETRIES = 2

log = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    """'2026-10-28T13:32:48+0300' или '2026-10-28' в datetime с поясом."""
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _iso_utc(value: Any) -> str | None:
    dt = _parse_dt(value)
    return dt.astimezone(UTC).isoformat() if dt else None


def _salary_text(v: dict[str, Any]) -> str:
    lo, hi = v.get("salary_min"), v.get("salary_max")
    if not lo and not hi:
        return ""
    if lo and hi and lo != hi:
        return f"от {lo} до {hi} руб."
    return f"от {lo or hi} руб."


def _uniq(items: list[Any]) -> list[str]:
    seen: list[str] = []
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _addresses(v: dict[str, Any]) -> list[str]:
    block = v.get("addresses") or {}
    items = block.get("address") if isinstance(block, dict) else None
    return _uniq([a.get("location", "") for a in items or [] if isinstance(a, dict)])


def build_text(v: dict[str, Any]) -> str:
    """Связный текст вакансии из структурных полей."""
    company = v.get("company") or {}
    req = v.get("requirement") or {}
    lines: list[str] = [str(v.get("job-name") or "").strip()]

    def add(label: str, value: Any) -> None:
        value = str(value).strip() if value not in (None, "", [], {}) else ""
        if value:
            lines.append(f"{label}: {value}")

    add("Работодатель", company.get("name"))
    add("Зарплата", _salary_text(v))
    add("Занятость", v.get("employment"))
    add("График", v.get("schedule"))
    add("Комментарий к графику", v.get("scheduleTypeComment"))
    add("Смена", ", ".join(_uniq(v.get("shift") or [])))
    add("Обязанности", v.get("duty"))
    add("Требования", v.get("requirements"))
    add("Квалификация", v.get("qualification"))
    if req.get("education"):
        add("Образование", req["education"])
    if req.get("experience") is not None:
        exp = req["experience"]
        add("Опыт работы", "не нужен" if exp == 0 else f"от {exp} лет")
    add("Навыки", ", ".join(_uniq(v.get("skills") or [])))
    add("Условия", v.get("conditions"))
    add("Льготы", v.get("benefit"))
    term = v.get("term")
    add("Условия оплаты", term.get("text") if isinstance(term, dict) else term)
    add("Адрес", "; ".join(_addresses(v)))
    for c in v.get("contact_list") or []:
        if isinstance(c, dict):
            add(str(c.get("contact_type") or "Контакт"), c.get("contact_value"))
    return "\n".join(lines)


def to_raw_post(v: dict[str, Any]) -> RawPost:
    return RawPost(
        source_type="trudvsem",
        source="trudvsem",
        external_id=str(v["id"]),
        url=v.get("vac_url") or f"https://trudvsem.ru/vacancy/card/{v.get('company', {}).get('companycode', '')}/{v['id']}",
        text=build_text(v),
        published_at=_iso_utc(v.get("date_modify")) or _iso_utc(v.get("creation-date")),
        image_urls=[],
        extra=v,
    )


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(RETRIES + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "results" in data:
                    return data
                log.warning("trudvsem: в ответе нет results: %s", str(data)[:200])
                return None
            log.warning("trudvsem: HTTP %s", resp.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("trudvsem: ошибка запроса: %s", exc)
        if attempt < RETRIES:
            time.sleep(PAUSE_SECONDS * (attempt + 1))
    return None


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit самых свежих вакансий ДНР. При ошибке источника возвращает пустой список."""
    if limit <= 0:
        return []
    own = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        # берём с запасом и сортируем сами, на случай смены порядка в API
        want = min(MAX_PAGE, max(limit * 3, 30))
        data = _get_json(client, API_URL.format(region=REGION_DNR), {"limit": want, "offset": 0})
        if not data:
            return []
        items = [
            x["vacancy"] for x in (data.get("results") or {}).get("vacancies") or []
            if isinstance(x, dict) and isinstance(x.get("vacancy"), dict) and x["vacancy"].get("id")
        ]
        items.sort(key=lambda v: _iso_utc(v.get("date_modify")) or _iso_utc(v.get("creation-date")) or "",
                   reverse=True)
        return [to_raw_post(v) for v in items[:limit]]
    finally:
        if own:
            client.close()
