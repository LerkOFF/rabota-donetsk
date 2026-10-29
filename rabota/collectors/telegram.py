"""Сборщик Telegram: публичное превью каналов https://t.me/s/<канал> без входа.

Превью отдаёт 20 последних постов, старее - параметр ?before=<id>. Посты идут от старых к новым,
поэтому свежие лежат в конце страницы. Закрытые чаты редиректятся на карточку канала без постов,
такие каналы дают пустой результат.
"""
from __future__ import annotations

import html
import logging
import random
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from rabota import config
from rabota.config import USER_AGENT
from rabota.models import RawPost

NAME = "telegram"
SOURCE_TYPE = "telegram"

# rabotadnr180, rabotadnrchat и другие чаты закрыты: превью не отдаёт постов
SOURCES = [
    "rabota_donetskv",
    "Vakansiii_dnr",
    "dnr_rabotaz",
    "dnr_rabotaq",
    "rabotadnrcom",
    "rabota_dnr_don",
    "chadachkadnr",
    "Vakansiidnr",
]

BASE_URL = "https://t.me/s/{channel}"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PAUSE = (0.5, 1.0)   # пауза между запросами, секунды
MAX_PAGES = 5        # сколько страниц превью максимум берём на один канал

log = logging.getLogger(__name__)

_BG_URL = re.compile(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
_VIEWS = re.compile(r"^\s*([\d.,]+)\s*([KkMm]?)\s*$")


class _Pacer:
    """Пауза 0.5-1 с между запросами: перед первым не ждём, перед каждым следующим ждём."""

    def __init__(self) -> None:
        self._started = False

    def wait(self) -> None:
        if self._started:
            time.sleep(random.uniform(*PAUSE))
        self._started = True


def _parse_views(raw: str) -> int | None:
    """'5.62K' -> 5620, '1,2M' -> 1200000."""
    m = _VIEWS.match(raw or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return round(num * mult)


def _node_text(node: LexborNode) -> str:
    """Текст узла: <br> становится переводом строки, разметки нет."""
    for br in node.css("br"):
        br.replace_with("\n")
    return node.text(deep=True, strip=False).replace("\xa0", " ").strip()


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


def _own_text_node(msg: LexborNode) -> LexborNode | None:
    """Текст самого поста, не цитаты ответа и не подписи под репостом."""
    for node in msg.css(".tgme_widget_message_text"):
        parent, nested = node.parent, False
        while parent is not None and parent.attributes.get("data-post") is None:
            cls = parent.attributes.get("class") or ""
            if "tgme_widget_message_reply" in cls or "tgme_widget_message_link_preview" in cls:
                nested = True
                break
            parent = parent.parent
        if not nested:
            return node
    return None


def _photo_urls(msg: LexborNode) -> list[str]:
    urls: list[str] = []
    for wrap in msg.css("a.tgme_widget_message_photo_wrap"):
        m = _BG_URL.search(wrap.attributes.get("style") or "")
        if m and m.group(1) not in urls:
            urls.append(m.group(1))
    return urls


def _external_links(text_node: LexborNode | None) -> list[str]:
    """Ссылки из текста (кнопки "Откликнуться", hh.ru и т.п.): в тексте остаётся только подпись."""
    if text_node is None:
        return []
    links: list[str] = []
    for a in text_node.css("a[href]"):
        href = html.unescape(a.attributes.get("href") or "")  # Telegram экранирует & дважды
        if href.startswith("http") and href not in links:
            links.append(href)
    return links


def parse_page(html: str, channel: str) -> list[RawPost]:
    """Разобрать страницу превью. Порядок как на странице: от старых к новым."""
    tree = LexborHTMLParser(html)
    posts: list[RawPost] = []
    for msg in tree.css("div.tgme_widget_message[data-post]"):
        data_post = msg.attributes.get("data-post") or ""
        owner, _, post_id = data_post.rpartition("/")
        if not post_id.isdigit() or owner.lower() != channel.lower():
            continue

        text_node = _own_text_node(msg)
        text = _node_text(text_node) if text_node is not None else ""

        time_node = msg.css_first("a.tgme_widget_message_date time[datetime]")
        published = _iso_utc(time_node.attributes.get("datetime") if time_node else None)

        extra: dict[str, Any] = {}
        views_node = msg.css_first(".tgme_widget_message_views")
        views = _parse_views(views_node.text()) if views_node else None
        if views is not None:
            extra["views"] = views
        links = _external_links(text_node)
        if links:
            extra["links"] = links
        preview = msg.css_first("a.tgme_widget_message_link_preview")
        if preview is not None and preview.attributes.get("href"):
            extra["link_preview"] = preview.attributes["href"]
        if msg.css_first(".tgme_widget_message_video_wrap, .tgme_widget_message_roundvideo_wrap"):
            extra["has_video"] = True
        if msg.css_first(".tgme_widget_message_forwarded_from"):
            extra["forwarded"] = True

        posts.append(RawPost(
            source_type=SOURCE_TYPE,
            source=channel,
            external_id=post_id,
            url=f"https://t.me/{channel}/{post_id}",
            text=text,
            published_at=published,
            image_urls=_photo_urls(msg),
            extra=extra,
        ))
    return posts


def _fetch_channel(client: httpx.Client, channel: str, limit: int, pacer: _Pacer) -> list[RawPost]:
    """Свежие посты одного канала, новые первыми. Листает ?before=<id>, пока не наберёт limit."""
    collected: dict[int, RawPost] = {}
    before: int | None = None
    for _ in range(MAX_PAGES):
        pacer.wait()
        params = {"before": before} if before else None
        resp = client.get(BASE_URL.format(channel=channel), params=params)
        resp.raise_for_status()
        page = parse_page(resp.text, channel)
        if not page:
            if before is None:
                log.warning("telegram %s: в превью нет постов (канал закрыт или пуст)", channel)
            break
        for post in page:
            collected[int(post.external_id)] = post
        oldest = min(int(p.external_id) for p in page)
        if len(collected) >= limit or oldest <= 1 or oldest == before:
            break
        before = oldest
    newest_first = [collected[i] for i in sorted(collected, reverse=True)]
    return newest_first[:limit]


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit свежих постов с каждого канала из SOURCES. Сломанный канал пропускается."""
    own_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            timeout=TIMEOUT,
            follow_redirects=True,
            proxy=config.telegram_proxy() or None,  # t.me с сервера может быть закрыт: ходим через прокси из .env
        )
    pacer = _Pacer()
    result: list[RawPost] = []
    try:
        for channel in SOURCES:
            try:
                posts = _fetch_channel(client, channel, limit, pacer)
            except Exception as exc:  # noqa: BLE001 - один сломанный канал не должен ронять остальные
                log.warning("telegram %s: пропускаю, ошибка: %s", channel, exc)
                continue
            log.info("telegram %s: %d постов", channel, len(posts))
            result.extend(posts)
    finally:
        if own_client:
            client.close()
    return result
