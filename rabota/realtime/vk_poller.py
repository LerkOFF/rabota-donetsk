"""Опрос VK: wall.get по пабликам из collectors.vk.SOURCES каждые несколько минут.

Разбор постов и вызов API берутся из rabota.collectors.vk (повторы при лимите, скрытие ключа в ошибках).
Берутся только посты новее последнего известного id паблика (таблица ingest_state). Лимит VK - 3 запроса
в секунду на ключ: между запросами пауза с случайным сдвигом, паблики идут по очереди.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from rabota import config
from rabota.collectors import vk
from rabota.realtime import state
from rabota.realtime.ingest import Ingestor

log = logging.getLogger("rabota.realtime.vk")

VK_INTERVAL = 240.0          # секунд между опросами всех пабликов (3-5 минут, сдвиг добавляет планировщик)
POLL_COUNT = 20              # постов на паблик при обычном опросе
FIRST_COUNT = 40             # при первом опросе паблика
PAUSE = (0.4, 0.8)           # пауза между запросами, секунды: меньше 3 запросов в секунду


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def is_configured() -> bool:
    return bool(config.get(vk.TOKEN_ENV).strip())


def _newest_id(items: list[dict[str, Any]]) -> int | None:
    """Самый большой id среди обычных постов: закреплённый пост курсор не двигает."""
    ids = [int(i["id"]) for i in items if isinstance(i, dict) and i.get("id") is not None and not i.get("is_pinned")]
    return max(ids) if ids else None


def poll_vk_sync(
    ingestor: Ingestor,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Один круг по пабликам. Сбой паблика пишется в состояние и не мешает остальным.

    Бросает VkTokenMissing без ключа и VkError, если не ответил ни один паблик (планировщик отступит).
    """
    token = vk._token()
    rng = rng or random.Random()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=config.VACANCY_TTL_DAYS)
    own = client is None
    http = client or httpx.Client(timeout=30.0, headers={"User-Agent": config.USER_AGENT}, follow_redirects=True)
    totals = {"posts": 0, "new": 0, "vacancies": 0, "from_images": 0, "duplicates": 0, "errors": 0, "failed": 0}
    try:
        for index, source in enumerate(vk.SOURCES):
            if index:
                _sleep(rng.uniform(*PAUSE))
            key = state.source_key("vk", source.domain)
            last_id = ingestor.call(state.get_last_id, key)
            try:
                response = vk._call_wall_get(http, token, source.domain, FIRST_COUNT if last_id is None else POLL_COUNT)
            except vk.VkError as exc:
                log.warning("VK %s: %s", source.domain, exc)
                ingestor.call(state.mark_error, key, str(exc))
                totals["failed"] += 1
                continue
            items = response.get("items") or []
            fresh = []
            for item in items:
                post = vk._parse_post(item, source)
                if post is None:
                    continue
                if last_id is not None and int(post.external_id) <= last_id:
                    continue
                if last_id is None and (published := _parse(post.published_at)) and published < cutoff:
                    continue
                fresh.append(post)
            fresh.sort(key=lambda p: int(p.external_id))
            result = ingestor.ingest_many(fresh) if fresh else {}
            for k, v in result.items():
                totals[k] = totals.get(k, 0) + v
            ingestor.call(state.mark_ok, key, _newest_id(items))
    finally:
        if own:
            http.close()
    if totals["failed"] == len(vk.SOURCES):
        raise vk.VkError("не ответил ни один паблик VK")
    return totals


def _parse(value: str | None) -> datetime | None:
    from rabota.lifecycle import parse_iso

    return parse_iso(value)


async def poll_vk(ingestor: Ingestor, **kwargs: Any) -> dict[str, int]:
    return await asyncio.to_thread(poll_vk_sync, ingestor, **kwargs)
