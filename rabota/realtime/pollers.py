"""Опрос открытых баз по интервалам через fetch() существующих сборщиков.

Сборщики синхронные (httpx), поэтому идут в потоке. Новые вакансии принимает Ingestor.
Пустой ответ источника не считается успехом: сборщики возвращают пустой список и при блокировке, и при
сбое, так что last_ok_at не обновляется, а health покажет просрочку. Исключение Авито: он закрыт капчей,
пустой ответ для него нормален, обходов блокировки нет.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rabota.models import RawPost
from rabota.realtime import state
from rabota.realtime.ingest import Ingestor

log = logging.getLogger("rabota.realtime.pollers")


@dataclass(frozen=True)
class PollerSpec:
    name: str                  # имя модуля в rabota.collectors
    label: str                 # для людей
    interval: float            # секунд
    limit: int                 # параметр limit для fetch
    allow_empty: bool = False  # пустой список - норма (Авито)

    @property
    def key(self) -> str:
        return state.source_key("poll", self.name)


POLLERS: tuple[PollerSpec, ...] = (
    PollerSpec("trudvsem", "Работа России", 15 * 60, 100),
    PollerSpec("hh", "hh.ru", 30 * 60, 50),
    PollerSpec("mczn", "Мой ЦЗН", 30 * 60, 50),
    PollerSpec("max", "MAX (зеркало)", 20 * 60, 5),
    PollerSpec("avito", "Авито", 60 * 60, 20, allow_empty=True),
)


def load_fetch(name: str) -> Callable[..., list[RawPost]]:
    return importlib.import_module(f"rabota.collectors.{name}").fetch


def poll_sync(spec: PollerSpec, ingestor: Ingestor, fetch: Callable[..., list[RawPost]] | None = None) -> dict[str, Any]:
    """Один опрос источника. Ошибка пишется в состояние и поднимается дальше (планировщик отступит)."""
    try:
        posts = (fetch or load_fetch(spec.name))(limit=spec.limit)
        if not posts:
            if spec.allow_empty:
                ingestor.call(state.mark_ok, spec.key)
                log.info("%s: пусто (для этого источника это нормально)", spec.name)
            else:
                ingestor.call(state.mark_empty, spec.key, "источник ничего не отдал: сбой или блокировка")
                log.warning("%s: пустой ответ", spec.name)
            return {"posts": 0, "new": 0, "vacancies": 0}
        totals = ingestor.ingest_many(posts)
        ingestor.call(state.mark_ok, spec.key)
    except Exception as exc:
        ingestor.call(state.mark_error, spec.key, f"{type(exc).__name__}: {exc}")
        raise
    log.info("%s: получено %d, новых %d, вакансий %d", spec.name, totals["posts"], totals["new"], totals["vacancies"])
    return totals


async def poll(spec: PollerSpec, ingestor: Ingestor, fetch: Callable[..., list[RawPost]] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(poll_sync, spec, ingestor, fetch)
