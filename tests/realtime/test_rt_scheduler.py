"""Расписание: интервалы, сдвиг, нарастающая пауза при ошибках, изоляция сбоев, остановка. Часы управляемые."""
from __future__ import annotations

import asyncio
import random

import pytest
from rt_helpers import FakeClock

from rabota.realtime.scheduler import Job, Scheduler, backoff_delay


def run_virtual(jobs, until, seed=7):
    """Прогнать расписание в виртуальном времени. Возвращает часы."""
    clock = FakeClock()
    sched = Scheduler(jobs, clock=clock, sleep=clock.sleep, rng=random.Random(seed))
    asyncio.run(clock.drive(sched, until))
    return clock


def recorder(times, clock_holder, fail_first=0):
    state = {"n": 0}

    async def run():
        state["n"] += 1
        times.append(clock_holder[0].t)
        if state["n"] <= fail_first:
            raise RuntimeError("сбой источника")

    return run


def with_clock(job_factory, until):
    """Собрать задачи с общими часами и прогнать. job_factory(holder) -> список задач."""
    holder = [FakeClock()]
    jobs = job_factory(holder)
    sched = Scheduler(jobs, clock=holder[0], sleep=holder[0].sleep, rng=random.Random(7))
    asyncio.run(holder[0].drive(sched, until))
    return jobs


def test_backoff_delay_doubles_and_caps():
    assert backoff_delay(0, 60, 3600) == 0
    assert [backoff_delay(n, 60, 3600) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert backoff_delay(10, 60, 3600) == 3600


def test_job_runs_every_interval():
    times = []
    [job] = with_clock(lambda h: [Job("a", 100.0, recorder(times, h), jitter=0.0, initial_delay=0.0)], 350)
    assert times == [0.0, 100.0, 200.0, 300.0]
    assert job.failures == 0 and job.last_error is None


def test_errors_back_off_then_recover():
    times = []
    [job] = with_clock(lambda h: [Job("a", 1000.0, recorder(times, h, fail_first=3), jitter=0.0, initial_delay=0.0,
                                      retry_base=60.0, backoff_cap=3600.0)], 1500)
    assert times == [0.0, 60.0, 180.0, 420.0, 1420.0]
    assert job.failures == 0                       # после успеха счётчик сброшен


def test_failure_state_is_visible():
    async def boom():
        raise ValueError("нет сети")

    job = Job("x", 100.0, boom, jitter=0.0)
    sched = Scheduler([job])
    assert asyncio.run(sched.run_once(job)) is False
    assert job.failures == 1 and "ValueError: нет сети" in job.last_error and job.last_ok is None


def test_default_retry_and_cap_formulas():
    rng = random.Random(1)
    job = Job("x", 900.0, lambda: None, jitter=0.0)
    job.failures = 1
    assert job.next_delay(False, rng) == 60.0          # первая повторная попытка: не позже минуты
    job.failures = 2
    assert job.next_delay(False, rng) == 120.0
    job.failures = 20
    assert job.next_delay(False, rng) == 3600.0        # потолок: 4 интервала, но не больше часа
    assert job.next_delay(True, rng) == 900.0
    short = Job("y", 30.0, lambda: None, jitter=0.0)
    short.failures = 9
    assert short.next_delay(False, rng) == 120.0       # потолок 4 интервала


def test_jitter_stays_within_bounds_and_varies():
    rng = random.Random(3)
    job = Job("x", 1000.0, lambda: None, jitter=0.2)
    delays = {job.next_delay(True, rng) for _ in range(50)}
    assert all(800.0 <= d <= 1200.0 for d in delays) and len(delays) > 10
    firsts = [job.first_delay(rng) for _ in range(20)]
    assert all(0 <= d <= 200 for d in firsts)
    assert Job("z", 10, lambda: None, initial_delay=3.0).first_delay(rng) == 3.0


def test_failing_job_does_not_stop_others():
    good_times = []

    async def bad():
        raise RuntimeError("всегда падает")

    jobs = with_clock(lambda h: [Job("bad", 10.0, bad, jitter=0.0, initial_delay=0.0, retry_base=10.0),
                                 Job("good", 10.0, recorder(good_times, h), jitter=0.0, initial_delay=0.0)], 55)
    assert good_times == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    assert jobs[0].failures >= 1 and jobs[1].failures == 0


def test_job_timeout_counts_as_error():
    async def hang():
        await asyncio.sleep(30)

    job = Job("slow", 100.0, hang, jitter=0.0, timeout=0.01)
    assert asyncio.run(Scheduler([job]).run_once(job)) is False
    assert "Timeout" in job.last_error


def test_stop_cancels_running_job():
    cancelled = []

    async def forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    job = Job("f", 10.0, forever, jitter=0.0, initial_delay=0.0)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(Scheduler([job]).run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert cancelled == [True]


@pytest.mark.parametrize("n_jobs", [0, 1])
def test_scheduler_with_few_jobs_stops(n_jobs):
    jobs = [Job("a", 10.0, lambda: asyncio.sleep(0), initial_delay=0.0)] * n_jobs

    async def go():
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(Scheduler(list(jobs)).run(stop), 2)

    asyncio.run(go())


def test_jobs_with_different_intervals_interleave():
    log = []

    def make(h):
        async def a():
            log.append(("a", h[0].t))

        async def b():
            log.append(("b", h[0].t))

        return [Job("a", 30.0, a, jitter=0.0, initial_delay=0.0), Job("b", 45.0, b, jitter=0.0, initial_delay=10.0)]

    with_clock(make, 100)
    assert [x for x in log if x[0] == "a"] == [("a", 0.0), ("a", 30.0), ("a", 60.0), ("a", 90.0)]
    assert [x for x in log if x[0] == "b"] == [("b", 10.0), ("b", 55.0), ("b", 100.0)]


def test_random_shift_spreads_first_runs():
    starts = []

    def make(h):
        return [Job(f"j{i}", 600.0, (lambda i=i: starts.append((i, h[0].t)) or asyncio.sleep(0)), jitter=0.2)
                for i in range(5)]

    with_clock(make, 200)
    times = [t for _, t in starts]
    assert len(times) == 5 and len(set(times)) == 5 and all(0 <= t <= 120 for t in times)
