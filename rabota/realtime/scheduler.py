"""Расписание: независимые задачи с интервалом, случайным сдвигом и нарастающей паузой при ошибках.

Каждая задача живёт в своём asyncio-цикле, поэтому сбой одной не задевает остальные. Часы и сон
подменяются (clock, sleep), так что расписание проверяется без реального ожидания.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("rabota.realtime.scheduler")

RETRY_BASE_MAX = 60.0       # первая повторная попытка после ошибки не позже чем через минуту
BACKOFF_CAP_MAX = 3600.0    # пауза после ошибок не растёт выше часа


def backoff_delay(failures: int, base: float, cap: float) -> float:
    """Пауза после failures подряд ошибок: base, 2*base, 4*base, ... не больше cap."""
    if failures <= 0:
        return 0.0
    return min(cap, base * (2 ** (failures - 1)))


@dataclass
class Job:
    """Периодическая задача. run - корутина без аргументов; исключение считается ошибкой."""

    name: str
    interval: float                          # секунды между успешными запусками
    run: Callable[[], Awaitable[Any]]
    jitter: float = 0.15                     # доля интервала: случайный сдвиг в обе стороны
    initial_delay: float | None = None       # до первого запуска; None - случайно в пределах jitter-доли интервала
    timeout: float | None = None             # предельное время одного запуска
    retry_base: float | None = None          # первая пауза после ошибки; по умолчанию min(interval, минута)
    backoff_cap: float | None = None         # потолок паузы; по умолчанию min(4*interval, час), не меньше retry_base
    runs: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)       # ошибок подряд
    last_ok: float | None = field(default=None, init=False)
    last_error: str | None = field(default=None, init=False)

    def next_delay(self, ok: bool, rng: random.Random) -> float:
        if ok:
            base = self.interval
        else:
            retry = self.retry_base if self.retry_base is not None else min(self.interval, RETRY_BASE_MAX)
            cap = self.backoff_cap if self.backoff_cap is not None else min(4 * self.interval, BACKOFF_CAP_MAX)
            base = backoff_delay(self.failures, retry, max(cap, retry))
        return max(0.0, base * (1 + rng.uniform(-self.jitter, self.jitter)))

    def first_delay(self, rng: random.Random) -> float:
        if self.initial_delay is not None:
            return self.initial_delay
        return rng.uniform(0, self.interval * self.jitter)


class Scheduler:
    def __init__(
        self,
        jobs: list[Job],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.jobs = jobs
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()

    async def run_once(self, job: Job) -> bool:
        """Один запуск задачи с учётом ошибок. Возвращает True при успехе."""
        job.runs += 1
        try:
            if job.timeout:
                await asyncio.wait_for(job.run(), job.timeout)
            else:
                await job.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - задача не должна ронять расписание
            job.failures += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("задача %s: ошибка (подряд %d): %s", job.name, job.failures, job.last_error)
            return False
        if job.failures:
            log.info("задача %s: снова работает после %d ошибок", job.name, job.failures)
        job.failures = 0
        job.last_error = None
        job.last_ok = self.clock()
        return True

    async def _loop(self, job: Job) -> None:
        delay = job.first_delay(self.rng)
        while True:
            if delay > 0:
                await self.sleep(delay)
            ok = await self.run_once(job)
            delay = job.next_delay(ok, self.rng)

    async def run(self, stop: asyncio.Event) -> None:
        """Крутить все задачи, пока не поднят stop; затем остановить их."""
        tasks = [asyncio.create_task(self._loop(j), name=f"job:{j.name}") for j in self.jobs]
        stopper = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            stopper.cancel()
            await asyncio.gather(stopper, *tasks, return_exceptions=True)
