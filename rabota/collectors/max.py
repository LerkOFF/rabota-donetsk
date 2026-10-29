"""Сборщик MAX: посты каналов через публичное зеркало каталога maxoteka.ru.

Что выяснено на практике (29.10.2026):
- Страница max.ru/<канал> без входа отдаёт только карточку канала (название, описание, число
  подписчиков), постов в ней нет, ни в разметке, ни во встроенном JSON. Публичного API постов нет.
- Зеркало https://maxoteka.ru/channel/<канал> показывает 5 последних постов канала. Дальше листать
  можно только через /channel/posts-more.php, который закрыт в robots.txt, поэтому не используем.
- В списке текст обрезается ("..."), а эмодзи заменены на svg. Полный текст, точное время (UTC)
  и картинка лежат на странице поста в JSON-LD (Article), её и берём, список служит запасным вариантом.
- Канал, которого нет в каталоге, или без постов в зеркале даёт пустой результат.
- Зеркало может отставать от самого канала и не знает каналов, которые в каталоге не проиндексированы.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from rabota.config import USER_AGENT
from rabota.models import RawPost

NAME = "max"
SOURCE_TYPE = "max"

SOURCES = [
    "channel_rabotadnr",
    "vakansii_donetsk_dnr",
    "vakansii_enakievo_dnr",
    "vakansii_gorlovka",
]

MIRROR = "https://maxoteka.ru"
CHANNEL_URL = MIRROR + "/channel/{channel}"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PAUSE = (0.5, 1.0)   # пауза между запросами, секунды

log = logging.getLogger(__name__)

_MAX_LINK = re.compile(r"^https://max\.ru/([^/]+)/([A-Za-z0-9_-]+)$")
_MID = re.compile(r"/post/(mid\.[0-9a-f]+)")
_LIST_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})")


class _Pacer:
    """Пауза 0.5-1 с между запросами: перед первым не ждём, перед каждым следующим ждём."""

    def __init__(self) -> None:
        self._started = False

    def wait(self) -> None:
        if self._started:
            time.sleep(random.uniform(*PAUSE))
        self._started = True


def _iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _list_date(value: str) -> str | None:
    """'28.10.2026 18:57' -> ISO. Зеркало показывает то же время, что и datePublished (UTC)."""
    m = _LIST_DATE.search(value or "")
    if not m:
        return None
    d, mo, y, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=UTC).isoformat()
    except ValueError:
        return None


def _list_text(node: LexborNode | None) -> str:
    """Текст из списка: svg-эмодзи выбрасываем, <br> в перевод строки."""
    if node is None:
        return ""
    for tag in node.css("svg"):
        tag.decompose()
    for br in node.css("br"):
        br.replace_with("\n")
    text = node.text(deep=True, strip=False).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_listing(html: str, channel: str) -> list[dict[str, Any]]:
    """Посты со страницы канала в зеркале, новые первыми. Текст в списке может быть обрезан."""
    tree = LexborHTMLParser(html)
    items: list[dict[str, Any]] = []
    for art in tree.css("article.ch-post"):
        mid = None
        mirror_path = None
        max_key = None
        for a in art.css("a[href]"):
            href = a.attributes.get("href") or ""
            m = _MID.search(href)
            if m and mid is None:
                mid, mirror_path = m.group(1), href
            mm = _MAX_LINK.match(href)
            if mm and mm.group(1).lower() == channel.lower() and max_key is None:
                max_key = mm.group(2)
        if mid is None and max_key is None:
            continue
        date_node = art.css_first(".ch-post-date")
        images = [
            img.attributes["src"]
            for img in art.css(".ch-post-media img[src]")
            if (img.attributes.get("src") or "").startswith("http")
        ]
        views = None
        for stat in art.css(".ch-post-stat"):
            if (stat.attributes.get("title") or "") == "Просмотры":
                digits = re.sub(r"\D", "", stat.text())
                views = int(digits) if digits else None
        items.append({
            "mid": mid,
            "max_key": max_key,
            "mirror_url": MIRROR + mirror_path if mirror_path else None,
            "date": _list_date(date_node.text() if date_node else ""),
            "text": _list_text(art.css_first(".ch-post-text")),
            "images": images,
            "views": views,
        })
    return items


def parse_post_page(html: str) -> dict[str, Any] | None:
    """Полный текст, время и картинка со страницы поста (JSON-LD Article). None, если не нашли."""
    tree = LexborHTMLParser(html)
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "Article":
            continue
        image = data.get("image")
        image_url = image.get("url") if isinstance(image, dict) else None
        return {
            "text": (data.get("articleBody") or "").strip(),
            "date": _iso_utc(data.get("datePublished")),
            "image": image_url if isinstance(image_url, str) else None,
        }
    return None


def _build_post(channel: str, item: dict[str, Any], full: dict[str, Any] | None) -> RawPost:
    text = full["text"] if full and full["text"] else item["text"]
    published = (full or {}).get("date") or item["date"]
    images = list(item["images"])
    if full and full.get("image") and not images:
        images = [full["image"]]
    if item["max_key"]:
        external_id = item["max_key"]
        url = f"https://max.ru/{channel}/{item['max_key']}"
    else:
        external_id = item["mid"]
        url = item["mirror_url"] or CHANNEL_URL.format(channel=channel)
    extra: dict[str, Any] = {"mirror": "maxoteka.ru"}
    if item["mid"]:
        extra["mid"] = item["mid"]
    if item["views"] is not None:
        extra["views"] = item["views"]
    if full is None:
        extra["text_may_be_truncated"] = True
    return RawPost(
        source_type=SOURCE_TYPE,
        source=channel,
        external_id=external_id,
        url=url,
        text=text,
        published_at=published,
        image_urls=images,
        extra=extra,
    )


def _fetch_channel(client: httpx.Client, channel: str, limit: int, pacer: _Pacer) -> list[RawPost]:
    pacer.wait()
    resp = client.get(CHANNEL_URL.format(channel=channel))
    resp.raise_for_status()
    items = parse_listing(resp.text, channel)[:limit]
    if not items:
        log.warning("max %s: в зеркале нет постов канала (не проиндексирован или пуст)", channel)
        return []
    posts: list[RawPost] = []
    for item in items:
        full = None
        if item["mirror_url"]:
            try:
                pacer.wait()
                page = client.get(item["mirror_url"])
                page.raise_for_status()
                full = parse_post_page(page.text)
            except Exception as exc:  # noqa: BLE001 - пост остаётся с обрезанным текстом из списка
                log.warning("max %s: страница поста %s не открылась: %s", channel, item["mid"], exc)
        posts.append(_build_post(channel, item, full))
    return posts


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit свежих постов с каждого канала из SOURCES (зеркало отдаёт не больше 5 на канал)."""
    own_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
    pacer = _Pacer()
    result: list[RawPost] = []
    try:
        for channel in SOURCES:
            try:
                posts = _fetch_channel(client, channel, limit, pacer)
            except Exception as exc:  # noqa: BLE001 - один сломанный канал не должен ронять остальные
                log.warning("max %s: пропускаю, ошибка: %s", channel, exc)
                continue
            log.info("max %s: %d постов", channel, len(posts))
            result.extend(posts)
    finally:
        if own_client:
            client.close()
    return result
