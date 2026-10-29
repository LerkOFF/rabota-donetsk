"""Сборщик VK: свежие посты пабликов через API wall.get.

Страницы VK отдают проверку от роботов, поэтому работаем только через API с ключом
доступа из переменной окружения VK_ACCESS_TOKEN (задаётся в .env на сервере).

Ключ передаётся заголовком Authorization и методом POST, поэтому не попадает в URL,
журналы httpx и тексты ошибок.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from rabota import config
from rabota.models import RawPost

NAME = "vk"

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/wall.get"
API_VERSION = "5.199"
TOKEN_ENV = "VK_ACCESS_TOKEN"

# Пауза между запросами к API, секунд.
REQUEST_PAUSE = 0.4
# Сколько раз повторить запрос при "слишком часто" (код 6) и сетевом сбое.
RATE_LIMIT_RETRIES = 3
NETWORK_RETRIES = 2
# Запас к count: закреплённый и рекламные посты отбрасываются, а свежих нужно limit.
COUNT_RESERVE = 5
MAX_COUNT = 100  # потолок wall.get


@dataclass(frozen=True)
class Source:
    """Паблик VK: короткое имя для API (domain) и пометка для разбора."""

    domain: str
    note: str = ""


SOURCES: list[Source] = [
    Source("dnr.work"),
    Source("club142496310"),
    Source("club68230454"),
    Source("rabotadnrr"),
    Source("public136489529"),
    Source("vakansiidnr"),
    Source(
        "public217224375",
        note="Мариуполь, почти весь набор на военный контракт: при разборе проверять вид поста",
    ),
]

# Понятные сообщения для кодов ошибок API.
ERROR_HINTS = {
    5: "ключ доступа недействителен или отозван",
    6: "слишком часто, превышена частота запросов",
    9: "слишком много однотипных действий",
    15: "нет доступа к стене паблика",
    18: "страница удалена или заблокирована",
    29: "достигнут лимит вызовов метода",
    100: "неверный параметр запроса",
}


class VkError(RuntimeError):
    """Ошибка сборщика VK. Текст не содержит ключ доступа."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class VkTokenMissing(VkError):
    """Не задан VK_ACCESS_TOKEN."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _token() -> str:
    token = config.get(TOKEN_ENV).strip()
    if not token:
        raise VkTokenMissing(
            f"не задан ключ доступа VK: добавьте {TOKEN_ENV} в .env (без него API wall.get отказывает)"
        )
    return token


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _call_wall_get(client: httpx.Client, token: str, domain: str, count: int) -> dict[str, Any]:
    """Один вызов wall.get с повторами при лимите частоты и сетевых сбоях. Возвращает response."""
    rate_attempts = 0
    net_attempts = 0
    while True:
        try:
            resp = client.post(
                API_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"v": API_VERSION, "domain": domain, "count": count},
            )
        except httpx.TransportError as exc:
            net_attempts += 1
            if net_attempts > NETWORK_RETRIES:
                raise VkError(
                    f"{domain}: сеть недоступна ({type(exc).__name__}: {_redact(str(exc), token)})"
                ) from None
            _sleep(1.0 * net_attempts)
            continue

        if resp.status_code != 200:
            raise VkError(f"{domain}: сервер VK ответил HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise VkError(f"{domain}: ответ VK не похож на JSON") from None
        if not isinstance(payload, dict):
            raise VkError(f"{domain}: неожиданный формат ответа VK")

        error = payload.get("error")
        if error:
            code = error.get("error_code") if isinstance(error, dict) else None
            if code == 6 and rate_attempts < RATE_LIMIT_RETRIES:
                rate_attempts += 1
                _sleep(1.0 * rate_attempts)
                continue
            hint = ERROR_HINTS.get(code, "ошибка API")
            msg = error.get("error_msg", "") if isinstance(error, dict) else ""
            raise VkError(f"{domain}: VK error_code={code}, {hint} ({_redact(str(msg), token)})", code=code)

        response = payload.get("response")
        if not isinstance(response, dict):
            raise VkError(f"{domain}: в ответе VK нет поля response")
        return response


def _iso_utc(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(int(timestamp), UTC).isoformat()


def _photo_urls(attachments: list[dict[str, Any]] | None) -> list[str]:
    """Для каждого фото самый большой размер по ширине. Остальные виды вложений пропускаем."""
    urls: list[str] = []
    for att in attachments or []:
        if att.get("type") != "photo":
            continue
        sizes = [s for s in (att.get("photo") or {}).get("sizes", []) if s.get("url")]
        if not sizes:
            continue
        best = max(sizes, key=lambda s: s.get("width") or 0)
        urls.append(best["url"])
    return urls


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _parse_post(item: dict[str, Any], source: Source) -> RawPost | None:
    """Пост wall.get в RawPost. None, если пост закреплённый, рекламный или пустой."""
    if item.get("is_pinned") or item.get("marked_as_ads"):
        return None

    text = item.get("text") or ""  # как есть, без обработки
    images = _photo_urls(item.get("attachments"))
    extra: dict[str, Any] = {"owner_id": item.get("owner_id")}

    # Репост: свой текст пустой, значит смысл поста в оригинале.
    if not text.strip():
        for origin in item.get("copy_history") or []:
            origin_text = (origin.get("text") or "").strip()
            origin_images = _photo_urls(origin.get("attachments"))
            if not origin_text and not origin_images:
                continue
            text = origin.get("text") or ""
            images += origin_images
            extra["repost_of"] = f"wall{origin.get('owner_id')}_{origin.get('id')}"
            break

    images = _unique(images)
    if not text.strip() and not images:
        return None  # нечего показывать и не из чего разбирать

    if source.note:
        extra["note"] = source.note

    owner_id = item.get("owner_id")
    post_id = item.get("id")
    return RawPost(
        source_type="vk",
        source=source.domain,
        external_id=str(post_id),
        url=f"https://vk.com/wall{owner_id}_{post_id}",
        text=text,
        published_at=_iso_utc(item.get("date")),
        image_urls=images,
        extra=extra,
    )


def _fetch_source(client: httpx.Client, token: str, source: Source, limit: int) -> list[RawPost]:
    count = min(limit + COUNT_RESERVE, MAX_COUNT)
    response = _call_wall_get(client, token, source.domain, count)
    posts: list[RawPost] = []
    for item in response.get("items", []):
        post = _parse_post(item, source)
        if post is not None:
            posts.append(post)
        if len(posts) >= limit:
            break
    return posts


def fetch(limit: int = 10, client: httpx.Client | None = None) -> list[RawPost]:
    """До limit свежих постов с каждого паблика из SOURCES.

    Сломанный паблик (нет доступа, сеть, лимиты) пропускается с записью в журнал.
    Если не получилось ни с одного паблика, бросает VkError. Без ключа бросает VkTokenMissing.
    """
    token = _token()
    own_client = client is None
    http = client or httpx.Client(
        timeout=30.0, headers={"User-Agent": config.USER_AGENT}, follow_redirects=True
    )
    posts: list[RawPost] = []
    errors: list[str] = []
    try:
        for index, source in enumerate(SOURCES):
            if index:
                _sleep(REQUEST_PAUSE)
            try:
                got = _fetch_source(http, token, source, limit)
            except VkError as exc:
                log.warning("VK %s пропущен: %s", source.domain, exc)
                errors.append(str(exc))
                continue
            log.info("VK %s: %d постов", source.domain, len(got))
            posts.extend(got)
    finally:
        if own_client:
            http.close()

    if errors and len(errors) == len(SOURCES):
        raise VkError("не удалось получить ни один паблик VK: " + "; ".join(errors))
    return posts
