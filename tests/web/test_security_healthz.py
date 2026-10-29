"""/healthz/deep: база, воркер, свежесть источников, без раскрытия лишнего."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rabota.db import connect
from rabota.ops import deep
from rabota.ops.logs import counters
from rabota.realtime import state
from rabota.realtime.health import write_heartbeat

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
ENV = {"VK_ACCESS_TOKEN": "x"}  # включает группу vk; открытые базы (poll) есть всегда


@pytest.fixture
def paths(tmp_path):
    db = tmp_path / "h.db"
    connect(db).close()
    return db, tmp_path / "worker.heartbeat"


def all_keys(env=ENV):
    from rabota.realtime.health import expected_sources

    return list(expected_sources(env))


def mark_all_ok(db: Path, age_minutes: float = 1.0, skip: tuple[str, ...] = ()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    for key in all_keys():
        if key not in skip:
            state.mark_ok(conn, key, None, NOW - timedelta(minutes=age_minutes))
    conn.close()


def test_group_state_rules():
    assert deep.group_state(5, 0) == "ok"
    assert deep.group_state(2, 2) == "down"
    assert deep.group_state(10, 3) == "degraded"
    assert deep.group_state(10, 6) == "down"
    assert deep.group_state(3, 1) == "degraded"


def test_everything_fresh_is_ok(paths):
    db, beat = paths
    mark_all_ok(db)
    write_heartbeat(beat, NOW - timedelta(seconds=30))
    report = deep.build_report(db, now=NOW, env=ENV, heartbeat=beat)
    assert report["status"] == "ok" and report["db"] == "ok" and report["worker"] == "ok"


def test_missing_worker_is_fail(paths):
    db, beat = paths
    mark_all_ok(db)
    report = deep.build_report(db, now=NOW, env=ENV, heartbeat=beat)
    assert report["status"] == "fail" and report["worker"] == "missing"


def test_one_stale_source_in_big_group_is_degraded(paths):
    db, beat = paths
    write_heartbeat(beat, NOW)
    keys = all_keys()
    poll = [k for k in keys if k.startswith("poll:")]
    mark_all_ok(db, skip=(poll[0],))
    report = deep.build_report(db, now=NOW, env=ENV, heartbeat=beat)
    assert report["groups"]["poll"]["state"] in ("degraded", "down")
    # группа VK целиком свежая
    assert report["groups"]["vk"]["state"] == "ok"
    if len(poll) >= 3:
        assert report["status"] == "degraded"


def test_whole_group_down_is_fail(paths):
    db, beat = paths
    write_heartbeat(beat, NOW)
    mark_all_ok(db, skip=tuple(k for k in all_keys() if k.startswith("vk:")))
    report = deep.build_report(db, now=NOW, env=ENV, heartbeat=beat)
    assert report["groups"]["vk"]["state"] == "down" and report["status"] == "fail"


def test_broken_database_is_fail(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 50)
    report = deep.build_report(bad, now=NOW, env=ENV, heartbeat=tmp_path / "hb")
    assert report["status"] == "fail" and report["db"] == "fail"


@pytest.fixture
def probe(paths, monkeypatch):
    db, beat = paths
    monkeypatch.setenv("HEARTBEAT_PATH", str(beat))
    monkeypatch.setenv("HEALTH_TOKEN", "s3cret-token")
    monkeypatch.delenv("VK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TG_API_ID", raising=False)
    app = FastAPI()
    deep.install(app, db)
    return db, beat, TestClient(app)


def test_route_public_view_hides_details(probe):
    db, _beat, client = probe
    r = client.get("/healthz/deep")
    body = r.json()
    assert r.status_code == 503  # воркера ещё нет
    assert set(body) == {"status", "db", "worker", "sources"}
    assert r.headers["cache-control"] == "no-store"
    text = r.text.lower()
    assert str(db) not in text and "error" not in text and "последн" not in text


def test_route_ok_returns_200_and_details_need_token(paths, monkeypatch):
    db, beat = paths
    monkeypatch.setenv("HEARTBEAT_PATH", str(beat))
    monkeypatch.setenv("HEALTH_TOKEN", "s3cret-token")
    monkeypatch.delenv("VK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TG_API_ID", raising=False)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    from rabota.realtime.health import expected_sources

    for key in expected_sources({}):
        state.mark_ok(conn, key)
    conn.close()
    write_heartbeat(beat)
    app = FastAPI()
    deep.install(app, db)
    client = TestClient(app)
    ok = client.get("/healthz/deep")
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    assert "counters" not in ok.json()
    wrong = client.get("/healthz/deep", headers={"X-Health-Token": "nope"})
    assert "counters" not in wrong.json()
    full = client.get("/healthz/deep", headers={"X-Health-Token": "s3cret-token"}).json()
    assert "counters" in full and "groups" in full and "uptime_seconds" in full


def test_errors_in_detailed_view_are_redacted(paths, monkeypatch):
    db, beat = paths
    monkeypatch.setenv("HEARTBEAT_PATH", str(beat))
    monkeypatch.delenv("VK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TG_API_ID", raising=False)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    from rabota.realtime.health import expected_sources

    keys = list(expected_sources({}))
    state.mark_error(conn, keys[0], "ошибка proxy socks5://user:pass@10.0.0.1:1080 token=ABCDEF")
    conn.close()
    report = deep.build_report(db, now=NOW + timedelta(days=2), env={}, heartbeat=beat)
    dump = json.dumps(report, ensure_ascii=False)
    assert "pass@" not in dump and "ABCDEF" not in dump


def test_result_is_cached_for_a_short_time(paths, monkeypatch):
    db, beat = paths
    monkeypatch.setenv("HEARTBEAT_PATH", str(beat))
    clock = {"t": 0.0}
    calls = []
    real = deep.build_report
    monkeypatch.setattr(deep, "build_report", lambda *a, **k: calls.append(1) or real(*a, **k))
    app = FastAPI()
    deep.install(app, db, clock=lambda: clock["t"])
    client = TestClient(app)
    client.get("/healthz/deep")
    client.get("/healthz/deep")
    assert len(calls) == 1
    clock["t"] = 100.0
    client.get("/healthz/deep")
    assert len(calls) == 2


def test_real_app_exposes_deep_and_plain_healthz(client):
    assert client.get("/healthz").text == "ok"
    r = client.get("/healthz/deep")
    assert r.status_code in (200, 503) and "status" in r.json()


def test_status_counter_middleware_counts_responses(db_path):
    from rabota.web.app import create_app

    counters.reset()
    app = create_app(db_path, site_url="https://example.test")
    with TestClient(app) as c:
        c.get("/")
        c.get("/nonexistent")
    snap = counters.snapshot()
    assert snap.get("http_2xx", 0) >= 1 and snap.get("http_4xx", 0) >= 1
