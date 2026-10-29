"""Приём одного поста: сохранить сырой, разобрать только его, сохранить вакансии, склеить дубли.

    ingest_post(conn, raw)      один пост, идемпотентно по (source, external_id)
    Ingestor(db_path)           потокобезопасная обёртка: своё соединение на вызов, одна запись за раз

Повторная публикация вакансии (тот же работодатель, другой пост или другой источник) склеивается с прежней
карточкой, и склейка переносит позднюю дату в `last_seen_at`: срок жизни начинается заново (см. lifecycle).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rabota import config
from rabota.db import connect, save_raw, save_vacancy
from rabota.models import RawPost, Vacancy
from rabota.pipeline.dedupe import dedupe
from rabota.pipeline.run import analyze

log = logging.getLogger("rabota.realtime.ingest")

BUSY_TIMEOUT_MS = 30_000


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    """База для воркера: WAL и ожидание блокировки, чтобы сайт и воркер не мешали друг другу."""
    conn = connect(path)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if str(path or config.DB_PATH) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def default_provider():
    """Провайдер картинок, как в `python -m rabota process`: модель по ключу, иначе готовая разметка."""
    from rabota.vision.providers import SidecarProvider, provider_from_env

    if config.get("LLM_PROVIDER") or config.get("LLM_API_KEY"):
        return provider_from_env()
    return SidecarProvider()


def new_image_vacancies(raw: RawPost, provider: Any, have: set[str]) -> list[Vacancy]:
    """Вакансии с картинок поста, кроме профессий, которые уже есть у этого поста (have меняется).

    Общая логика для `python -m rabota process` и приёма в реальном времени. Сбой распознавания не роняет разбор.
    """
    from rabota.vision.extract import vacancies_from_images

    try:
        found = vacancies_from_images(raw, provider)
    except Exception as exc:  # noqa: BLE001 - картинка не должна ломать разбор текста
        log.warning("картинки %s: %s", raw.url, exc)
        return []
    out: list[Vacancy] = []
    for vac in found:
        if vac.profession in have:
            continue
        have.add(vac.profession)
        out.append(vac)
    return out


def ingest_post(
    conn: sqlite3.Connection, raw: RawPost, provider: Any = None, *, run_dedupe: bool = True
) -> dict[str, Any]:
    """Сохранить и разобрать один пост. Возвращает статистику.

    {"new": False, ...} если пост уже в базе (ничего не меняется). Иначе: kind поста, число вакансий
    (в том числе с картинок: from_images) и число новых дублей. run_dedupe=False оставляет склейку вызывающему
    (удобно для пачки: склеить один раз в конце).
    """
    stats: dict[str, Any] = {"new": False, "kind": None, "vacancies": 0, "from_images": 0, "duplicates": 0}
    exists = conn.execute(
        "SELECT 1 FROM raw_posts WHERE source = ? AND external_id = ?", (raw.source, raw.external_id)
    ).fetchone()
    if exists:
        return stats

    # сначала всё, что может быть долгим или упасть (разбор, картинки), потом короткая запись в базу
    analysis = analyze(raw)
    have = {v.profession for v in analysis.vacancies}
    from_images: list[Vacancy] = []
    if raw.image_urls:
        from_images = new_image_vacancies(raw, provider if provider is not None else default_provider(), have)

    if not save_raw(conn, raw):
        return stats  # другой процесс успел раньше
    stats["new"] = True
    stats["kind"] = analysis.kind if not from_images else "vacancy"
    for vac in analysis.vacancies + from_images:
        save_vacancy(conn, vac)
        stats["vacancies"] += 1
    stats["from_images"] = len(from_images)
    if stats["vacancies"] and run_dedupe:
        stats["duplicates"] = dedupe(conn)
    return stats


_TOTALS = ("posts", "new", "vacancies", "from_images", "duplicates", "errors")


class Ingestor:
    """Приём постов из потоков и корутин: одна запись за раз, своё соединение на каждый вызов."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        provider: Any = None,
        opener: Callable[[Path | str | None], sqlite3.Connection] = open_db,
    ) -> None:
        self.db_path = db_path
        self._provider = provider
        self._opener = opener
        self._lock = threading.Lock()

    @property
    def provider(self) -> Any:
        if self._provider is None:
            self._provider = default_provider()
        return self._provider

    @contextmanager
    def session(self):
        with self._lock:
            conn = self._opener(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """fn(conn, *args, **kwargs) под блокировкой, в одном соединении."""
        with self.session() as conn:
            return fn(conn, *args, **kwargs)

    async def acall(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.call, fn, *args, **kwargs)

    def ingest_many(self, posts: Iterable[RawPost]) -> dict[str, int]:
        """Принять пачку: каждый пост отдельно, склейка дублей один раз в конце.

        Сбой разбора одного поста пишется в журнал и не мешает остальным. Ошибка базы (sqlite3.OperationalError)
        поднимается наверх: пачку повторят, посты идемпотентны.
        """
        totals = dict.fromkeys(_TOTALS, 0)
        with self.session() as conn:
            saved = 0
            for raw in posts:
                totals["posts"] += 1
                try:
                    res = ingest_post(conn, raw, self.provider, run_dedupe=False)
                except sqlite3.OperationalError:
                    raise
                except Exception as exc:  # noqa: BLE001 - один плохой пост не роняет пачку
                    totals["errors"] += 1
                    log.warning("пост %s/%s не принят: %s: %s", raw.source, raw.external_id, type(exc).__name__, exc)
                    continue
                totals["new"] += int(res["new"])
                totals["vacancies"] += res["vacancies"]
                totals["from_images"] += res["from_images"]
                saved += res["vacancies"]
            if saved:
                totals["duplicates"] = dedupe(conn)
        return totals

    async def aingest_many(self, posts: Iterable[RawPost]) -> dict[str, int]:
        return await asyncio.to_thread(self.ingest_many, list(posts))


def add_totals(into: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for k, v in more.items():
        into[k] = into.get(k, 0) + v
    return into
