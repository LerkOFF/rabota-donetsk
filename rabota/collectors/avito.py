"""Сборщик Авито: раздел "Вакансии" Донецка и Макеевки.

Статус доступа (проверено 29.10.2026): Авито отвечает на автоматические запросы кодом 429 и
страницей "Доступ ограничен: проблема с IP" с капчей. Это относится и к обычному httpx с браузерными
заголовками, и к публичному читателю страниц r.jina.ai (он получает ту же страницу блокировки).
Капчу и блокировку сборщик не обходит: при отказе он пишет предупреждение в журнал, прекращает
обход (остальные разделы за тем же IP закрыты так же) и возвращает то, что уже успел собрать,
обычно пустой список.

Разбор страницы рассчитан на два вида разметки, чтобы пережить смену вёрстки:
1) встроенный JSON `window.__initialData__` (список объявлений ищется по ключам id и urlPath);
2) HTML-карточки `[data-marker="item"]`.

Телефоны на Авито скрыты за кнопкой, сборщик их не добывает.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from rabota.models import RawPost

log = logging.getLogger(__name__)

NAME = "avito"
BASE_URL = "https://www.avito.ru"

SOURCES = [
    "https://www.avito.ru/donetsk/vakansii",
    "https://www.avito.ru/donetck/vakansii",
    "https://www.avito.ru/makeevka/vakansii",
    "https://www.avito.ru/donetsk/vakansii/podrabotka-ASgBAgICAUTs0hIC",
    "https://www.avito.ru/donetck/vakansii/chastota_vyplat:_kazhdyy_den-ASgBAgICAUSGwQ2s7zc",
]

# Пауза между запросами к разделам, секунды. Без агрессии: один запрос за раз.
PAUSE_SECONDS = 4.0
TIMEOUT = 20.0
# Сортировка Авито "по дате", чтобы первыми шли свежие объявления.
SORT_BY_DATE = {"s": "104"}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Донецк живёт по московскому времени (UTC+3), Авито показывает время в нём.
LOCAL_TZ = timezone(timedelta(hours=3))

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_BLOCK_MARKERS = ("Доступ ограничен", "проблема с IP", "form-captcha", "h-captcha", "geetest_captcha")


class AvitoBlocked(Exception):
    """Авито отказал в доступе: 429, 403 или страница с капчей."""


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def is_blocked(status_code: int, html: str) -> bool:
    """Страница блокировки: код 429/403 или разметка капчи без объявлений."""
    if status_code in (403, 429):
        return True
    head = html[:60000]
    has_items = 'data-marker="item"' in head or "__initialData__" in head
    return not has_items and any(marker in head for marker in _BLOCK_MARKERS)


def _abs_url(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    return urljoin(BASE_URL + "/", href)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _item_id_from_url(url: str) -> str:
    match = re.search(r"_(\d+)(?:[/?#]|$)", urlparse(url).path + "/")
    return match.group(1) if match else ""


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date_text(text: str, now: datetime | None = None) -> str | None:
    """Дата из подписи Авито ("Сегодня в 12:30", "3 часа назад", "15 октября") в ISO UTC."""
    raw = _clean(text).lower()
    if not raw:
        return None
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)

    rel = re.search(r"(\d+)\s*(минут|мин|час|дн|день|дня|недел)", raw)
    if rel and "назад" in raw:
        n = int(rel.group(1))
        unit = rel.group(2)
        if unit.startswith("мин"):
            delta = timedelta(minutes=n)
        elif unit.startswith("час"):
            delta = timedelta(hours=n)
        elif unit.startswith("недел"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=n)
        return _iso_utc(now_local - delta)
    if "минуту назад" in raw:
        return _iso_utc(now_local - timedelta(minutes=1))
    if "час назад" in raw:
        return _iso_utc(now_local - timedelta(hours=1))
    if "неделю назад" in raw:
        return _iso_utc(now_local - timedelta(weeks=1))

    clock = re.search(r"(\d{1,2}):(\d{2})", raw)
    hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (0, 0)

    day: datetime | None = None
    if "сегодня" in raw:
        day = now_local
    elif "вчера" in raw:
        day = now_local - timedelta(days=1)
    else:
        dm = re.search(r"(\d{1,2})\s+([а-я]+)", raw)
        if dm and dm.group(2) in _MONTHS:
            month = _MONTHS[dm.group(2)]
            year = now_local.year
            try:
                candidate = datetime(year, month, int(dm.group(1)), tzinfo=LOCAL_TZ)
            except ValueError:
                return None
            if candidate > now_local + timedelta(days=1):
                candidate = candidate.replace(year=year - 1)
            day = candidate
    if day is None:
        return None
    return _iso_utc(day.replace(hour=hour, minute=minute, second=0, microsecond=0))


def _ms_to_iso(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 1e11:  # миллисекунды
        number /= 1000
    return _iso_utc(datetime.fromtimestamp(number, tz=UTC))


# ---------------------------------------------------------------------------
# Разбор встроенного JSON
# ---------------------------------------------------------------------------

def _extract_initial_data(tree: HTMLParser) -> Any | None:
    for script in tree.css("script"):
        body = script.text(deep=False) or ""
        if "__initialData__" not in body:
            continue
        _, _, tail = body.partition("__initialData__")
        tail = tail.lstrip().lstrip("=").lstrip()
        try:
            # raw_decode читает одно значение хвостовые инструкции не мешают инструкции скрипта
            value, _ = json.JSONDecoder().raw_decode(tail)
            if isinstance(value, str):  # Авито кладёт JSON в виде строки с процентным кодированием
                value = json.loads(unquote(value))
            return value
        except (ValueError, TypeError):
            continue
    return None


def _find_items(node: Any, found: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Рекурсивно ищет объявления: словари с ключами id и urlPath."""
    if found is None:
        found = []
    if isinstance(node, dict):
        if "id" in node and "urlPath" in node and "title" in node:
            found.append(node)
        else:
            for value in node.values():
                _find_items(value, found)
    elif isinstance(node, list):
        for value in node:
            _find_items(value, found)
    return found


def _json_images(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for image in item.get("images") or []:
        if isinstance(image, str):
            urls.append(_abs_url(image))
        elif isinstance(image, dict):
            sized = {k: v for k, v in image.items() if isinstance(v, str)}
            if not sized:
                continue

            def area(key: str) -> int:
                m = re.match(r"(\d+)x(\d+)", key)
                return int(m.group(1)) * int(m.group(2)) if m else 0

            best = max(sized, key=area)
            urls.append(_abs_url(sized[best]))
    return urls


def _json_item_to_post(item: dict[str, Any], section: str) -> RawPost | None:
    url_path = str(item.get("urlPath") or "")
    if not url_path:
        return None
    url = _abs_url(url_path)
    external_id = str(item.get("id") or _item_id_from_url(url))
    title = _clean(item.get("title"))
    description = _clean(item.get("description"))

    price = item.get("priceDetailed") or {}
    price_text = _clean(price.get("string")) if isinstance(price, dict) else ""
    price_value = price.get("value") if isinstance(price, dict) else None

    geo = item.get("geo") or {}
    address = _clean(geo.get("formattedAddress")) if isinstance(geo, dict) else ""
    district = ""
    if isinstance(geo, dict):
        refs = geo.get("geoReferences") or []
        if refs and isinstance(refs[0], dict):
            district = _clean(refs[0].get("content"))
    location = item.get("location") or {}
    city = _clean(location.get("name")) if isinstance(location, dict) else ""
    if not district:
        loc_name = (item.get("addressDetailed") or {}).get("locationName") if isinstance(
            item.get("addressDetailed"), dict) else ""
        district = _clean(loc_name)

    images = _json_images(item)
    return _make_post(
        section=section, external_id=external_id, url=url, title=title, price_text=price_text,
        price_value=price_value, description=description, city=city, district=district,
        address=address, category=_clean((item.get("category") or {}).get("name"))
        if isinstance(item.get("category"), dict) else "",
        published_at=_ms_to_iso(item.get("sortTimeStamp")), date_text="", images=images,
    )


# ---------------------------------------------------------------------------
# Разбор HTML-карточек
# ---------------------------------------------------------------------------

def _text_of(node: Node, selector: str) -> str:
    found = node.css_first(selector)
    return _clean(found.text(separator=" ")) if found else ""


def _html_item_to_post(card: Node, section: str, now: datetime | None) -> RawPost | None:
    link = card.css_first('[data-marker="item-title"]') or card.css_first("a[href]")
    href = (link.attributes.get("href") if link else "") or ""
    if not href:
        return None
    url = _abs_url(href.split("?")[0])
    external_id = card.attributes.get("data-item-id") or _item_id_from_url(url)
    title = _text_of(card, '[itemprop="name"]') or (_clean(link.text()) if link else "")

    price_meta = card.css_first('meta[itemprop="price"]')
    price_value: Any = price_meta.attributes.get("content") if price_meta else None
    price_text = _text_of(card, '[data-marker="item-price"]')

    description = _text_of(card, '[data-marker="item-description"]')
    address = _text_of(card, '[data-marker="item-address"]')
    date_text = _text_of(card, '[data-marker="item-date"]')

    images: list[str] = []
    for img in card.css("img"):
        src = img.attributes.get("src") or ""
        if not src:
            srcset = img.attributes.get("srcset") or ""
            src = srcset.split(",")[-1].strip().split(" ")[0] if srcset else ""
        if src and not src.startswith("data:"):
            images.append(_abs_url(src))

    return _make_post(
        section=section, external_id=external_id, url=url, title=title, price_text=price_text,
        price_value=price_value, description=description, city="", district=address,
        address=address, category="", published_at=parse_date_text(date_text, now),
        date_text=date_text, images=images,
    )


# ---------------------------------------------------------------------------
# Сборка RawPost
# ---------------------------------------------------------------------------

def _make_post(
    *, section: str, external_id: str, url: str, title: str, price_text: str, price_value: Any,
    description: str, city: str, district: str, address: str, category: str,
    published_at: str | None, date_text: str, images: list[str],
) -> RawPost | None:
    if not external_id or not title:
        return None
    try:
        salary = int(float(price_value)) if price_value not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        salary = None
    text = "\n".join(part for part in (title, price_text, description, address) if part)
    return RawPost(
        source_type="avito",
        source=NAME,
        external_id=str(external_id),
        url=url,
        text=text,
        published_at=published_at,
        image_urls=images,
        extra={
            "section": section,
            "title": title,
            "price": price_text,
            "price_value": salary,
            "currency": "RUB" if salary or price_text else None,
            "description": description,
            "city": city,
            "district": district,
            "address": address,
            "category": category,
            "date_text": date_text,
        },
    )


def parse_listing(html: str, section: str = "", now: datetime | None = None) -> list[RawPost]:
    """Разбирает страницу раздела в список RawPost в порядке страницы. Без сети."""
    tree = HTMLParser(html)
    posts: list[RawPost] = []

    data = _extract_initial_data(tree)
    if data is not None:
        for item in _find_items(data):
            post = _json_item_to_post(item, section)
            if post:
                posts.append(post)

    if not posts:
        for card in tree.css('[data-marker="item"]'):
            post = _html_item_to_post(card, section, now)
            if post:
                posts.append(post)

    seen: set[str] = set()
    unique: list[RawPost] = []
    for post in posts:
        if post.external_id not in seen:
            seen.add(post.external_id)
            unique.append(post)
    return unique


# ---------------------------------------------------------------------------
# Сбор
# ---------------------------------------------------------------------------

def _get_section(client: httpx.Client, url: str) -> str:
    """Скачивает страницу раздела. При блокировке бросает AvitoBlocked."""
    response = client.get(url, params=SORT_BY_DATE)
    if is_blocked(response.status_code, response.text):
        raise AvitoBlocked(f"{url}: код {response.status_code}")
    response.raise_for_status()
    return response.text


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit свежих объявлений на каждый раздел.

    При блокировке Авито (429, капча) обход прекращается, в журнал пишется предупреждение,
    возвращается уже собранное (в бою чаще всего пустой список).
    """
    own_client = client is None
    http = client or httpx.Client(
        headers=BROWSER_HEADERS,
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    result: list[RawPost] = []
    seen: set[str] = set()
    try:
        for index, section in enumerate(SOURCES):
            if index:
                time.sleep(PAUSE_SECONDS)
            try:
                html = _get_section(http, section)
            except AvitoBlocked as exc:
                log.warning(
                    "Авито закрыл доступ (ограничение запросов или капча), обход остановлен, "
                    "капчу не обходим: %s", exc,
                )
                break
            except httpx.HTTPError as exc:
                log.warning("Авито: раздел %s не загрузился: %s", section, exc)
                continue

            fresh = parse_listing(html, section)[:limit]
            if not fresh:
                log.warning("Авито: в разделе %s не найдено объявлений, возможно сменилась разметка", section)
            for post in fresh:
                if post.external_id not in seen:  # один и тот же город бывает в двух разделах
                    seen.add(post.external_id)
                    result.append(post)
    finally:
        if own_client:
            http.close()
    return result
