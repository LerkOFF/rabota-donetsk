"""Воркер: `python -m rabota worker`.

Один asyncio-процесс: слушатель Telegram (если настроен), опрос VK и открытых баз, ежедневное expire,
сердцебиение раз в минуту. Один сбойный источник остальных не роняет. SIGTERM и SIGINT завершают работу.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rabota import config
from rabota.lifecycle import expire, purge
from rabota.realtime import pollers, vk_poller
from rabota.realtime.health import HEARTBEAT_EVERY, heartbeat_path, write_heartbeat
from rabota.realtime.ingest import Ingestor
from rabota.realtime.scheduler import Job, Scheduler
from rabota.realtime.telegram_listener import TelegramListener, TelegramNotConfigured, TelegramSettings

log = logging.getLogger("rabota.realtime.worker")

EXPIRE_EVERY = 24 * 3600.0
EXPIRE_FIRST_DELAY = 120.0
TELEGRAM_CRASH_PAUSE = 60.0


def cleanup_media(directory: Path, max_age_days: float, now: datetime | None = None) -> int:
    """Удалить копии фото старше max_age_days (по времени изменения). Возвращает число удалённых файлов."""
    if not directory.is_dir():
        return 0
    border = (now or datetime.now(UTC)).timestamp() - max_age_days * 86400
    removed = 0
    for path in directory.glob("*.jpg"):
        try:
            if path.stat().st_mtime < border:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def expire_job(conn: Any, media_dir: Path | None = None, now: datetime | None = None) -> dict[str, int]:
    """Скрыть истёкшие вакансии, удалить давно скрытые и старые копии фото."""
    result = {"скрыто": expire(conn, now=now), "удалено": purge(conn, now=now)}
    keep = config.VACANCY_TTL_DAYS + config.EXPIRED_KEEP_DAYS + 1
    if media_dir is not None:
        result["фото"] = cleanup_media(media_dir, keep, now)
    return result


def build_jobs(ingestor: Ingestor, env: Mapping[str, str], media_dir: Path | None = None) -> list[Job]:
    """Задачи расписания: сердцебиение, VK, открытые базы, expire."""
    beat = heartbeat_path()

    async def heartbeat() -> None:
        await asyncio.to_thread(write_heartbeat, beat)

    jobs = [Job("heartbeat", HEARTBEAT_EVERY, heartbeat, jitter=0.0, initial_delay=0.0)]

    if vk_poller.is_configured():
        async def run_vk() -> None:
            await vk_poller.poll_vk(ingestor)

        jobs.append(Job("vk", vk_poller.VK_INTERVAL, run_vk, timeout=300, initial_delay=5.0, jitter=0.25))
    else:
        log.warning("VK пропущен: не задан VK_ACCESS_TOKEN")

    for index, spec in enumerate(pollers.POLLERS):
        async def run_poll(spec: pollers.PollerSpec = spec) -> None:
            await pollers.poll(spec, ingestor)

        jobs.append(Job(spec.name, spec.interval, run_poll, timeout=600, initial_delay=15.0 + 20.0 * index))

    if (env.get("WORKER_EXPIRE") or "1").strip() != "0":
        async def run_expire() -> None:
            res = await ingestor.acall(expire_job, media_dir)
            log.info("expire: %s", res)

        jobs.append(Job("expire", EXPIRE_EVERY, run_expire, jitter=0.01, initial_delay=EXPIRE_FIRST_DELAY,
                        retry_base=600.0))
    return jobs


async def _telegram_task(listener: TelegramListener, stop: asyncio.Event) -> None:
    """Слушатель живёт сам; если он упал неожиданно, перезапускаем с паузой, остальной воркер не задет."""
    while not stop.is_set():
        try:
            await listener.run(stop)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("слушатель Telegram упал, перезапуск через %.0f с", TELEGRAM_CRASH_PAUSE)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), TELEGRAM_CRASH_PAUSE)


def make_listener(env: Mapping[str, str], ingestor: Ingestor) -> TelegramListener | None:
    try:
        settings = TelegramSettings.from_env(env)
    except TelegramNotConfigured as exc:
        log.warning("Telegram пропущен: %s", exc)
        return None
    return TelegramListener(settings, ingestor)


async def run_worker(
    stop: asyncio.Event,
    *,
    env: Mapping[str, str] | None = None,
    ingestor: Ingestor | None = None,
    listener: TelegramListener | None | bool = True,
    jobs: list[Job] | None = None,
) -> None:
    """Работать до stop. listener=True: создать из окружения, None или False: без Telegram."""
    import os

    env = os.environ if env is None else env
    ingestor = ingestor or Ingestor(config.DB_PATH)
    if listener is True:
        listener = make_listener(env, ingestor)
    from rabota.vision.images import media_dir

    scheduler = Scheduler(jobs if jobs is not None else build_jobs(ingestor, env, media_dir()))
    tasks = [asyncio.create_task(scheduler.run(stop), name="scheduler")]
    if listener:
        tasks.append(asyncio.create_task(_telegram_task(listener, stop), name="telegram"))
    log.info("воркер запущен: задач %d, Telegram %s", len(scheduler.jobs), "включён" if listener else "выключен")
    await stop.wait()
    log.info("остановка воркера")
    _, pending = await asyncio.wait(tasks, timeout=30)
    for t in pending:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "telethon", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    setup_logging()

    async def amain() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    started = time.monotonic()
    asyncio.run(amain())
    log.info("воркер остановлен через %.0f с", time.monotonic() - started)
    return 0
