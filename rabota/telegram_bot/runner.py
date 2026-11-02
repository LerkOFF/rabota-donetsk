"""Фоновые задачи Telegram для воркера: приём команд бота (getUpdates), доставка подписчикам, автопостинг в канал.

Приём команд идёт длинным опросом `getUpdates`: публичный порт и webhook не нужны. Смещение хранится в `ingest_state`
(ключ `bot:offset`, поле last_id), поэтому после перезапуска обновления не теряются и не обрабатываются дважды.
Без токена бота ничего не запускается.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rabota import config
from rabota.alerts import conf
from rabota.db import connect
from rabota.realtime import state
from rabota.telegram_bot import bot as bot_mod
from rabota.telegram_bot.api import BotApi, BotApiError, HttpBotApi

log = logging.getLogger("rabota.telegram_bot")

OFFSET_KEY = "bot:offset"
POLL_KEY = "bot:poll"
BACKOFF_MIN, BACKOFF_MAX = 5.0, 120.0
CONFLICT_PAUSE = 30.0
STATE_EVERY = 120.0

ApiFactory = Callable[[conf.TelegramConfig], BotApi]
Clock = Callable[[], datetime]


def default_api(cfg: conf.TelegramConfig) -> BotApi:
    return HttpBotApi(cfg.token, cfg.proxy)


def _open(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _handle_batch(db_path: Path | str, updates: list[dict[str, Any]], site_url: str, now: datetime) -> tuple[list[bot_mod.Reply], int]:
    """Разобрать пачку обновлений в потоке: ответы и новый offset. Ошибка одного обновления остальные не задевает."""
    conn = _open(db_path)
    try:
        replies: list[bot_mod.Reply] = []
        last = 0
        for update in updates:
            last = max(last, int(update.get("update_id") or 0))
            try:
                replies += bot_mod.plan(conn, update, site_url=site_url, now=now)
            except Exception:
                log.exception("обновление %s не разобрано", update.get("update_id"))
        if last:
            state.mark_ok(conn, OFFSET_KEY, last)
        return replies, last
    finally:
        conn.close()


def _load_offset(db_path: Path | str) -> int:
    conn = _open(db_path)
    try:
        last = state.get_last_id(conn, OFFSET_KEY)
        return (last + 1) if last else 0
    finally:
        conn.close()


def _mark(db_path: Path | str, error: str | None = None) -> None:
    conn = _open(db_path)
    try:
        if error is None:
            state.mark_ok(conn, POLL_KEY)
        else:
            state.mark_error(conn, POLL_KEY, error)
    finally:
        conn.close()


def bot_enabled(db_path: Path | str) -> bool:
    conn = _open(db_path)
    try:
        return bool(conf.setting(conn, "tg_bot_enabled"))
    finally:
        conn.close()


async def run_bot(
    stop: asyncio.Event, cfg: conf.TelegramConfig, db_path: Path | str, *, site_url: str,
    api_factory: ApiFactory = default_api, clock: Clock = lambda: datetime.now(UTC), poll_timeout: int = 25,
    idle_sleep: float = 30.0,
) -> None:
    """Принимать команды бота, пока не поднят stop. Сетевые сбои: нарастающая пауза. Токен не действует: остановка."""
    if not cfg.bot_ready:
        return
    api = api_factory(cfg)
    delay = BACKOFF_MIN
    last_mark = 0.0
    loop = asyncio.get_running_loop()
    try:
        with contextlib.suppress(BotApiError):
            await api.delete_webhook()  # с включённым webhook getUpdates не работает
        offset = await asyncio.to_thread(_load_offset, db_path)
        log.info("бот Telegram запущен (%s)", cfg.bot_username)
        while not stop.is_set():
            if not await asyncio.to_thread(bot_enabled, db_path):
                await _sleep(stop, idle_sleep)
                continue
            try:
                updates = await api.get_updates(offset, poll_timeout)
            except BotApiError as exc:
                if exc.code == 401:
                    log.error("токен бота не действует, приём команд остановлен")
                    await asyncio.to_thread(_mark, db_path, "токен бота не действует")
                    return
                pause = CONFLICT_PAUSE if exc.code == 409 else max(delay, float(exc.retry_after))
                log.warning("getUpdates: %s, пауза %.0f с", exc, pause)
                await asyncio.to_thread(_mark, db_path, str(exc)[:200])
                delay = min(delay * 2, BACKOFF_MAX)
                await _sleep(stop, pause)
                continue
            delay = BACKOFF_MIN
            if updates:
                replies, last = await asyncio.to_thread(_handle_batch, db_path, updates, site_url, clock())
                offset = max(offset, last + 1)
                await bot_mod.send_replies(api, replies)
            if updates or loop.time() - last_mark > STATE_EVERY:
                await asyncio.to_thread(_mark, db_path)
                last_mark = loop.time()
            if not updates and poll_timeout == 0:
                await _sleep(stop, 1.0)
    finally:
        with contextlib.suppress(Exception):
            await api.aclose()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


def make_bot_task(stop: asyncio.Event, env: Any = None, db_path: Path | str | None = None) -> asyncio.Task[None] | None:
    """Задача приёма команд для воркера или None, если бот не настроен."""
    cfg = conf.TelegramConfig.from_env(env)
    if not cfg.bot_ready:
        log.warning("бот Telegram пропущен: не задан TG_BOT_TOKEN")
        return None
    return asyncio.create_task(
        run_bot(stop, cfg, db_path or config.DB_PATH, site_url=config.site_url()), name="telegram-bot",
    )
