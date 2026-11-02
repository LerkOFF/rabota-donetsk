"""Задачи воркера для подписок: рассылка писем, доставка в Telegram, автопостинг в канал.

Каждая задача открывает своё соединение, ошибка не роняет воркер (планировщик отступит с нарастающей паузой), итог
пишется в `ingest_state` с ключами `job:alerts-email`, `job:alerts-telegram`, `job:channel`.
Рассылка писем запускается, только если почта настроена (см. `conf.mail_enabled`); без неё задача не создаётся.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rabota import config
from rabota.accounts import csrf as csrf_mod
from rabota.accounts.mailer import Mailer, make_mailer
from rabota.alerts import conf, dispatch, service
from rabota.realtime import state
from rabota.realtime.scheduler import Job
from rabota.telegram_bot import channel, deliver
from rabota.telegram_bot.api import BotApi
from rabota.telegram_bot.runner import ApiFactory, default_api
from rabota.web.queries import open_conn

log = logging.getLogger("rabota.alerts")

EMAIL_EVERY = 300.0
TELEGRAM_EVERY = 120.0
CHANNEL_EVERY = 600.0
KEY_EMAIL, KEY_TELEGRAM, KEY_CHANNEL = "job:alerts-email", "job:alerts-telegram", "job:channel"

Clock = Callable[[], datetime]


def _open(db_path: Path | str) -> sqlite3.Connection:
    conn = open_conn(db_path)  # с функцией rfold для поиска по кириллице
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def email_job_sync(
    db_path: Path | str, mailer: Mailer, secret: bytes, site_url: str, now: datetime, sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    conn = _open(db_path)
    try:
        service.purge(conn, now)
        result = dispatch.run_email_cycle(conn, mailer, now, site_url=site_url, secret=secret, sleep=sleep)
        if result["errors"] and not result["sent"]:
            state.mark_error(conn, KEY_EMAIL, f"не отправлено писем: {result['errors']}")
        else:
            state.mark_ok(conn, KEY_EMAIL)
        return result
    finally:
        conn.close()


def telegram_job_sync(
    db_path: Path | str, cfg: conf.TelegramConfig, api_factory: ApiFactory, site_url: str, now: datetime,
) -> dict[str, Any]:
    """Доставка подписчикам и автопостинг в канал в одном потоке со своим циклом событий (клиент создаётся здесь)."""

    async def go() -> dict[str, Any]:
        api: BotApi = api_factory(cfg)
        conn = _open(db_path)
        try:
            out: dict[str, Any] = {}
            out["deliver"] = await deliver.run_telegram_cycle(conn, api, now, site_url=site_url)
            if out["deliver"]["errors"] and not out["deliver"]["sent"]:
                state.mark_error(conn, KEY_TELEGRAM, "доставка в Telegram не удалась")
            else:
                state.mark_ok(conn, KEY_TELEGRAM)
            return out
        finally:
            conn.close()
            await api.aclose()

    return asyncio.run(go())


def channel_job_sync(
    db_path: Path | str, cfg: conf.TelegramConfig, api_factory: ApiFactory, site_url: str, now: datetime,
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        api: BotApi = api_factory(cfg)
        conn = _open(db_path)
        try:
            result = await channel.run_channel_cycle(conn, api, cfg.channel_id, now, site_url=site_url)
            if result["error"]:
                state.mark_error(conn, KEY_CHANNEL, result["error"])
            else:
                state.mark_ok(conn, KEY_CHANNEL)
            return result
        finally:
            conn.close()
            await api.aclose()

    return asyncio.run(go())


def build_alert_jobs(
    db_path: Path | str | None = None, env: Mapping[str, str] | None = None, *, mailer: Mailer | None = None,
    api_factory: ApiFactory = default_api, clock: Clock = lambda: datetime.now(UTC), site_url: str | None = None,
) -> list[Job]:
    """Задачи подписок для воркера. Что не настроено, того в списке нет (причина в журнале)."""
    path = Path(db_path) if db_path else config.DB_PATH
    base = (site_url or config.site_url()).rstrip("/")
    cfg = conf.TelegramConfig.from_env(env)
    jobs: list[Job] = []

    if mailer is None and not conf.mail_enabled(env):
        log.warning("рассылка писем по подпискам пропущена: почта не настроена (MAIL_BACKEND=smtp и SMTP_HOST)")
    else:
        try:
            the_mailer = mailer or make_mailer(env, outbox_dir=path.parent / "outbox")
        except ValueError as exc:
            log.warning("рассылка писем по подпискам пропущена: %s", exc)
        else:
            secret = csrf_mod.load_secret(None, path.parent)

            async def run_email() -> None:
                res = await asyncio.to_thread(email_job_sync, path, the_mailer, secret, base, clock())
                if res["sent"] or res["errors"]:
                    log.info("рассылка писем: %s", res)

            jobs.append(Job("alerts-email", EMAIL_EVERY, run_email, jitter=0.05, initial_delay=45.0, timeout=900))

    if cfg.bot_ready:
        async def run_telegram() -> None:
            res = await asyncio.to_thread(telegram_job_sync, path, cfg, api_factory, base, clock())
            if res["deliver"]["sent"] or res["deliver"]["errors"]:
                log.info("доставка в Telegram: %s", res["deliver"])

        jobs.append(Job("alerts-telegram", TELEGRAM_EVERY, run_telegram, jitter=0.05, initial_delay=60.0, timeout=600))
    else:
        log.warning("доставка в Telegram пропущена: не задан TG_BOT_TOKEN")

    if cfg.channel_ready:
        async def run_channel() -> None:
            res = await asyncio.to_thread(channel_job_sync, path, cfg, api_factory, base, clock())
            if res["posted"]:
                log.info("пост в канал: рубрика %s", res["posted"])

        jobs.append(Job("channel", CHANNEL_EVERY, run_channel, jitter=0.05, initial_delay=90.0, timeout=300))
    else:
        log.warning("автопостинг в канал пропущен: не заданы TG_BOT_TOKEN и TG_CHANNEL_ID")
    return jobs
