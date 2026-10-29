"""Сборщик: портал Мой ЦЗН (m-czn.ru), вакансии ДНР.

Список: https://m-czn.ru/vacancy/dnr?limit=N&limitstart=M, по умолчанию сортировка "Дата (Новые)".
В карточке списка есть название, зарплата от-до, работодатель, город, график, опыт и дата обновления.
За описанием, адресом и контактами открываем страницу каждой вакансии. Если страница вакансии не
открылась, остаются данные карточки списка.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from rabota.config import USER_AGENT
from rabota.models import RawPost

NAME = "mczn"
SOURCES = ("mczn",)

BASE_URL = "https://m-czn.ru"
LIST_URL = BASE_URL + "/vacancy/dnr"
TIMEOUT = 25.0
PAUSE_SECONDS = 1.0     # вежливая пауза между запросами
PAGE_SIZE = 25          # допустимые значения сайта: 5, 10, 15, 20, 25, 50
FETCH_DETAILS = True

log = logging.getLogger(__name__)

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_ru_date(text: str) -> str | None:
    """'Обновлено: 29 октября 2026' в '2026-10-29T00:00:00+00:00' (на сайте только дата)."""
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.lower())
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        dt = datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), tzinfo=UTC)
    except ValueError:
        return None
    return dt.isoformat()


def _salary(text: str) -> tuple[int | None, int | None]:
    lo = re.search(r"от\s*(\d[\d\s]*)", text)
    hi = re.search(r"до\s*(\d[\d\s]*)", text)

    def to_int(m: re.Match[str] | None) -> int | None:
        return int(re.sub(r"\s", "", m.group(1))) if m else None

    return to_int(lo), to_int(hi)


def parse_list(html: str) -> list[dict[str, Any]]:
    """Карточки со страницы списка."""
    tree = HTMLParser(html)
    out: list[dict[str, Any]] = []
    for card in tree.css("div.product"):
        cls = card.attributes.get("class") or ""
        m = re.search(r"productitem_(\d+)", cls)
        link = card.css_first("div.name a")
        if not m or not link:
            continue
        href = link.attributes.get("href") or ""
        price_node = card.css_first("div.jshop_price")
        sal_min, sal_max = _salary(_clean(price_node.text()) if price_node else "")
        item: dict[str, Any] = {
            "id": m.group(1),
            "title": _clean(link.text()),
            "url": urljoin(BASE_URL, href),
            "salary_min": sal_min,
            "salary_max": sal_max,
            "employer": None,
            "city": None,
            "schedule": None,
            "experience": None,
            "updated": None,
        }
        plain: list[str] = []
        for div in card.css("div.extra_fields > div"):
            city = div.css_first(".product-categories .category-name")
            label = div.css_first(".label-name")
            data = div.css_first(".data")
            if city:
                item["city"] = _clean(city.text())
            elif label and data:
                if "опыт" in label.text().lower():
                    item["experience"] = _clean(data.text())
            elif data:
                plain.append(_clean(data.text()))
        if plain:
            item["employer"] = plain[0]
        if len(plain) > 1:
            item["schedule"] = plain[1]
        date_node = card.css_first("div.date-add")
        item["updated"] = parse_ru_date(date_node.text()) if date_node else None
        out.append(item)
    return out


def _value_text(node: Node) -> str:
    """Значение поля, переносы строк превращаем в пробелы."""
    return _clean(node.text(separator=" "))


def parse_detail(html: str) -> dict[str, Any]:
    """Описание, поля работодателя и характеристики со страницы вакансии."""
    tree = HTMLParser(html)
    detail: dict[str, Any] = {"description": "", "fields": {}, "datePosted": None}
    desc = tree.css_first(".jshop_prod_description")
    if desc:
        detail["description"] = _clean(desc.text())
    for el in tree.css("div.extra_fields_el"):
        name = el.css_first(".extra_fields_name")
        value = el.css_first(".extra_fields_value")
        if name and value:
            key = _clean(name.text()).rstrip(":")
            detail["fields"][key] = _value_text(value)
    posted = tree.css_first('meta[itemprop="datePosted"]')
    if posted:
        detail["datePosted"] = posted.attributes.get("content")
    src = tree.css_first(".manufacturer-name")
    if src:
        detail["original_source"] = _clean(src.text())
    return detail


def build_text(item: dict[str, Any], detail: dict[str, Any] | None) -> str:
    fields = (detail or {}).get("fields", {})
    lines = [item["title"]]

    def add(label: str, value: Any) -> None:
        if value:
            lines.append(f"{label}: {value}")

    lo, hi = item.get("salary_min"), item.get("salary_max")
    if lo and hi and lo != hi:
        add("Зарплата", f"от {lo} до {hi} руб.")
    elif lo or hi:
        add("Зарплата", f"от {lo or hi} руб.")
    add("Работодатель", fields.get("Предприятие") or item.get("employer"))
    add("Город", item.get("city"))
    add("График", fields.get("График работы") or item.get("schedule"))
    add("Сфера деятельности", fields.get("Сфера деятельности"))
    add("Опыт работы", fields.get("Опыт работы") or item.get("experience"))
    add("Образование", fields.get("Образование"))
    if detail and detail.get("description"):
        add("Обязанности", detail["description"])
    add("Адрес", fields.get("Адрес"))
    add("Контакты", fields.get("Контакты"))
    return "\n".join(lines)


def to_raw_post(item: dict[str, Any], detail: dict[str, Any] | None) -> RawPost:
    published = item.get("updated")
    if detail and detail.get("datePosted"):
        published = parse_ru_date_iso(detail["datePosted"]) or published
    extra = {k: v for k, v in item.items() if k not in ("id", "url", "title")}
    extra["title"] = item["title"]
    if detail:
        extra["description"] = detail.get("description", "")
        extra["fields"] = detail.get("fields", {})
        extra["original_source"] = detail.get("original_source")
        extra["date_posted"] = detail.get("datePosted")
    extra["date_only"] = True   # на сайте дата без времени
    return RawPost(
        source_type="mczn",
        source="m-czn.ru",
        external_id=item["id"],
        url=item["url"],
        text=build_text(item, detail),
        published_at=published,
        image_urls=[],
        extra=extra,
    )


def parse_ru_date_iso(value: str) -> str | None:
    """'2026-10-29' в ISO с нулевым временем."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
    except (ValueError, TypeError):
        return None


def _get(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> str | None:
    try:
        resp = client.get(url, params=params)
    except httpx.HTTPError as exc:
        log.warning("mczn: ошибка запроса %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("mczn: HTTP %s для %s", resp.status_code, url)
        return None
    return resp.text


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit свежих вакансий ДНР. Ошибка страницы не роняет сборщик."""
    if limit <= 0:
        return []
    own = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        while len(items) < limit:
            size = min(PAGE_SIZE, max(limit - len(items), 5))
            html = _get(client, LIST_URL, {"limit": size, "limitstart": start})
            if html is None:
                break
            page = [c for c in parse_list(html) if c["id"] not in seen]
            if not page:
                break
            for c in page:
                seen.add(c["id"])
                items.append(c)
            start += size
            if len(items) < limit:
                time.sleep(PAUSE_SECONDS)
        items = items[:limit]

        posts: list[RawPost] = []
        for item in items:
            detail = None
            if FETCH_DETAILS:
                time.sleep(PAUSE_SECONDS)
                html = _get(client, item["url"])
                if html is not None:
                    try:
                        detail = parse_detail(html)
                    except Exception as exc:  # noqa: BLE001 - разбор не должен ронять остальные вакансии
                        log.warning("mczn: не разобрана страница %s: %s", item["url"], exc)
            posts.append(to_raw_post(item, detail))
        return posts
    finally:
        if own:
            client.close()
