"""Здоровье источников, сердцебиение, воркер и команды командной строки."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from rabota import __main__ as cli
from rabota import config
from rabota.db import connect, save_vacancy
from rabota.models import Vacancy
from rabota.realtime import health, state, worker
from rabota.realtime.pollers import POLLERS
from rabota.realtime.scheduler import Job

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
EXPECTED = {"poll:hh": ("hh.ru", 7200.0), "vk:dnr.work": ("VK dnr.work", 960.0)}


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def beat(tmp_path):
    p = tmp_path / "worker.heartbeat"
    health.write_heartbeat(p, NOW)
    return p


# ---------------------------------------------------------------- health

def test_all_fresh_is_ok(conn, beat):
    state.mark_ok(conn, "poll:hh", None, NOW - timedelta(minutes=10))
    state.mark_ok(conn, "vk:dnr.work", 5, NOW - timedelta(minutes=2))
    rep = health.check_health(conn, NOW, EXPECTED, beat)
    assert rep.ok and [s.status for s in rep.sources] == ["ok", "ok"]
    text = health.format_report(rep)
    assert "Итог: всё свежее." in text and "воркер работает" in text


def test_stale_source_fails_with_russian_summary(conn, beat):
    state.mark_ok(conn, "poll:hh", None, NOW - timedelta(hours=3))
    state.mark_ok(conn, "vk:dnr.work", 5, NOW - timedelta(minutes=2))
    rep = health.check_health(conn, NOW, EXPECTED, beat)
    assert not rep.ok
    by = {s.key: s for s in rep.sources}
    assert by["poll:hh"].status == "stale" and by["vk:dnr.work"].status == "ok"
    text = health.format_report(rep)
    assert "ПРОСРОЧЕН" in text and "hh.ru" in text and "Итог: проблем 1." in text and "3 ч" in text


def test_never_polled_is_a_problem(conn, beat):
    rep = health.check_health(conn, NOW, EXPECTED, beat)
    assert {s.status for s in rep.sources} == {"never"} and not rep.ok
    assert "НЕТ ДАННЫХ" in health.format_report(rep) and "никогда" in health.format_report(rep)


def test_error_without_success_is_never_and_shows_error(conn, beat):
    state.mark_error(conn, "poll:hh", "ConnectError: нет сети", NOW)
    state.mark_ok(conn, "vk:dnr.work", 1, NOW)
    rep = health.check_health(conn, NOW, EXPECTED, beat)
    row = next(s for s in rep.sources if s.key == "poll:hh")
    assert row.status == "never" and "ConnectError" in health.format_report(rep)


def test_fresh_success_with_old_error_text_is_ok_but_error_cleared(conn, beat):
    state.mark_error(conn, "poll:hh", "сбой", NOW - timedelta(minutes=5))
    state.mark_ok(conn, "poll:hh", None, NOW - timedelta(minutes=1))
    state.mark_ok(conn, "vk:dnr.work", 1, NOW)
    assert health.check_health(conn, NOW, EXPECTED, beat).ok


def test_threshold_boundary(conn, beat):
    state.mark_ok(conn, "vk:dnr.work", 1, NOW - timedelta(seconds=960))
    state.mark_ok(conn, "poll:hh", None, NOW)
    assert health.check_health(conn, NOW, EXPECTED, beat).ok
    state.mark_ok(conn, "vk:dnr.work", 1, NOW - timedelta(seconds=961))
    assert not health.check_health(conn, NOW, EXPECTED, beat).ok


def test_threshold_formula():
    assert health.threshold_for(900) == 3600
    assert health.threshold_for(60) == 600
    assert health.threshold_for(3600) == 14400


def test_sources_outside_expected_are_ignored(conn, beat):
    state.mark_ok(conn, "poll:hh", None, NOW)
    state.mark_ok(conn, "vk:dnr.work", 1, NOW)
    state.mark_error(conn, "telegram:old_channel", "давно не нужен")
    assert health.check_health(conn, NOW, EXPECTED, beat).ok


def test_heartbeat_states(conn, tmp_path):
    state.mark_ok(conn, "poll:hh", None, NOW)
    state.mark_ok(conn, "vk:dnr.work", 1, NOW)
    path = tmp_path / "hb"
    missing = health.check_health(conn, NOW, EXPECTED, path)
    assert missing.worker_status == "missing" and not missing.ok and "НЕ ЗАПУЩЕН" in health.format_report(missing)
    health.write_heartbeat(path, NOW - timedelta(minutes=10))
    old = health.check_health(conn, NOW, EXPECTED, path)
    assert old.worker_status == "stale" and "МОЛЧИТ" in health.format_report(old)
    health.write_heartbeat(path, NOW - timedelta(seconds=30))
    assert health.check_health(conn, NOW, EXPECTED, path).ok


def test_heartbeat_file_roundtrip_and_garbage(tmp_path):
    p = tmp_path / "d" / "hb"
    health.write_heartbeat(p, NOW)
    assert json.loads(p.read_text())["pid"] == os.getpid()
    assert health.read_heartbeat(p) == NOW
    p.write_text("мусор")
    assert health.read_heartbeat(p) is None
    p.write_text("2026-10-29T12:00:00+00:00")        # старый формат: просто время
    assert health.read_heartbeat(p) == NOW


def test_human_age():
    assert health.human_age(None) == "никогда"
    assert health.human_age(30) == "30 с назад"
    assert health.human_age(600) == "10 мин назад"
    assert health.human_age(3 * 3600 + 120) == "3 ч 2 мин назад"
    assert health.human_age(5 * 86400) == "5 дн назад"


def test_expected_sources_follow_environment(tmp_path):
    base = health.expected_sources({})
    assert set(base) == {p.key for p in POLLERS}
    with_vk = health.expected_sources({"VK_ACCESS_TOKEN": "x"})
    assert "vk:dnr.work" in with_vk and with_vk["vk:dnr.work"][1] == 960
    (tmp_path / "sess.session").write_bytes(b"")
    tg = health.expected_sources({"TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION_PATH": str(tmp_path / "sess")})
    assert "telegram:rabota_donetskv" in tg and tg["telegram:rabota_donetskv"][1] == 1200
    assert not any(k.startswith("telegram:") for k in base)


def test_health_command_exit_codes(tmp_path, capsys, monkeypatch):
    assert cli.main(["health"]) == 1
    out = capsys.readouterr().out
    assert "ВОРКЕР НЕ ЗАПУЩЕН" in out and "Итог: проблем" in out

    c = connect(config.DB_PATH)
    for spec in POLLERS:
        state.mark_ok(c, spec.key)
    c.close()
    health.write_heartbeat(health.heartbeat_path())
    assert cli.main(["health"]) == 0
    assert "Итог: всё свежее." in capsys.readouterr().out


def test_health_json(capsys):
    assert cli.main(["health", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["worker"] == "missing" and data["sources"][0]["key"].startswith("poll:")


def test_health_heartbeat_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "x" / "hb"))
    assert health.heartbeat_path() == tmp_path / "x" / "hb"


# ---------------------------------------------------------------- воркер

def test_build_jobs_without_vk_and_with_everything(tmp_path, ingestor, monkeypatch):
    names = [j.name for j in worker.build_jobs(ingestor, {})]
    assert names == ["heartbeat", "trudvsem", "hh", "mczn", "max", "avito", "expire"]
    monkeypatch.setenv("VK_ACCESS_TOKEN", "x")
    jobs = worker.build_jobs(ingestor, {})
    assert [j.name for j in jobs][:2] == ["heartbeat", "vk"]
    by = {j.name: j.interval for j in jobs}
    assert by["trudvsem"] == 900 and by["hh"] == 1800 and by["mczn"] == 1800 and by["max"] == 1200
    assert by["avito"] == 3600 and by["expire"] == 86400 and by["heartbeat"] == 60 and 180 <= by["vk"] <= 300


def test_expire_can_be_switched_off(ingestor):
    assert "expire" not in [j.name for j in worker.build_jobs(ingestor, {"WORKER_EXPIRE": "0"})]


def test_poller_jobs_call_their_own_source(ingestor, monkeypatch):
    called = []

    async def fake_poll(spec, ing, fetch=None):
        called.append(spec.name)

    monkeypatch.setattr(worker.pollers, "poll", fake_poll)

    async def go():
        for job in worker.build_jobs(ingestor, {}):
            if job.name in {p.name for p in POLLERS}:
                await job.run()

    asyncio.run(go())
    assert called == ["trudvsem", "hh", "mczn", "max", "avito"]


def test_heartbeat_job_writes_file(ingestor):
    job = worker.build_jobs(ingestor, {})[0]
    asyncio.run(job.run())
    assert health.read_heartbeat(health.heartbeat_path()) is not None


def old_vacancy(conn):
    return save_vacancy(conn, Vacancy(profession="бариста", published_at="2020-01-01T00:00:00+00:00"))


def test_expire_job_hides_purges_and_cleans_media(tmp_path, db_path):
    c = connect(db_path)
    fresh = save_vacancy(c, Vacancy(profession="повар", published_at=datetime.now(UTC).isoformat()))
    old = old_vacancy(c)
    media = tmp_path / "media"
    media.mkdir()
    (media / "old.jpg").write_bytes(b"x")
    (media / "new.jpg").write_bytes(b"x")
    ancient = time.time() - 200 * 86400
    os.utime(media / "old.jpg", (ancient, ancient))
    res = worker.expire_job(c, media)
    assert res["фото"] == 1 and not (media / "old.jpg").exists() and (media / "new.jpg").exists()
    assert res["удалено"] == 1 and c.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (old,)).fetchone()[0] == 0
    assert c.execute("SELECT status FROM vacancies WHERE id=?", (fresh,)).fetchone()[0] == "active"
    c.close()


def test_expire_job_runs_through_ingestor(ingestor, db_path):
    c = connect(db_path)
    vid = old_vacancy(c)
    c.close()
    job = next(j for j in worker.build_jobs(ingestor, {}) if j.name == "expire")
    asyncio.run(job.run())
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == 0


def test_cleanup_media_missing_dir(tmp_path):
    assert worker.cleanup_media(tmp_path / "none", 10) == 0


def test_make_listener_skips_without_settings(ingestor, caplog):
    with caplog.at_level("WARNING"):
        assert worker.make_listener({}, ingestor) is None
    assert "Telegram пропущен" in caplog.text and "TG_API_ID" in caplog.text


def test_make_listener_skips_without_session(ingestor, tmp_path, caplog):
    env = {"TG_API_ID": "1", "TG_API_HASH": "secret-hash-value", "TG_SESSION_PATH": str(tmp_path / "none")}
    with caplog.at_level("WARNING"):
        assert worker.make_listener(env, ingestor) is None
    assert "tg-login" in caplog.text and "secret-hash-value" not in caplog.text


def test_make_listener_builds_when_configured(ingestor, tmp_path):
    (tmp_path / "s.session").write_bytes(b"")
    env = {"TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION_PATH": str(tmp_path / "s")}
    assert worker.make_listener(env, ingestor) is not None


def test_worker_runs_other_jobs_without_telegram_and_stops_on_event(ingestor, caplog):
    ticks = []

    async def tick():
        ticks.append(1)

    async def go():
        stop = asyncio.Event()
        jobs = [Job("t", 0.01, tick, jitter=0.0, initial_delay=0.0)]
        task = asyncio.create_task(worker.run_worker(stop, env={}, ingestor=ingestor, listener=None, jobs=jobs))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert len(ticks) >= 3


def test_one_failing_job_does_not_kill_worker(ingestor):
    ok = []

    async def bad():
        raise RuntimeError("упал")

    async def good():
        ok.append(1)

    async def go():
        stop = asyncio.Event()
        jobs = [Job("bad", 0.01, bad, jitter=0.0, initial_delay=0.0, retry_base=0.01),
                Job("good", 0.01, good, jitter=0.0, initial_delay=0.0)]
        task = asyncio.create_task(worker.run_worker(stop, env={}, ingestor=ingestor, listener=None, jobs=jobs))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert len(ok) >= 3


class FakeListener:
    def __init__(self, crash_first=False):
        self.crash_first = crash_first
        self.runs = 0
        self.stopped = False

    async def run(self, stop):
        self.runs += 1
        if self.crash_first and self.runs == 1:
            raise RuntimeError("неожиданный сбой")
        await stop.wait()
        self.stopped = True


def test_worker_runs_and_stops_telegram_listener(ingestor):
    fl = FakeListener()

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(stop, env={}, ingestor=ingestor, listener=fl, jobs=[]))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert fl.runs == 1 and fl.stopped


def test_crashed_listener_is_restarted(ingestor, monkeypatch):
    monkeypatch.setattr(worker, "TELEGRAM_CRASH_PAUSE", 0.01)
    fl = FakeListener(crash_first=True)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(stop, env={}, ingestor=ingestor, listener=fl, jobs=[]))
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert fl.runs == 2 and fl.stopped


def test_main_wires_sigterm(monkeypatch):
    import signal

    seen = {}

    async def fake_run(stop, **kw):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 2)
        seen["stopped"] = True

    monkeypatch.setattr(worker, "run_worker", fake_run)
    assert worker.main() == 0 and seen == {"stopped": True}


def test_cli_registers_worker_health_and_login(monkeypatch, capsys):
    monkeypatch.setattr("rabota.realtime.worker.main", lambda: 0)
    assert cli.main(["worker"]) == 0
    assert cli.main(["tg-login"]) == 2        # без TG_API_ID сеть не трогается
    assert "Не настроено" in capsys.readouterr().out


def test_no_secrets_in_default_logging(ingestor):
    worker.setup_logging()
    import logging

    assert logging.getLogger("httpx").level == logging.WARNING and logging.getLogger("telethon").level == logging.WARNING
