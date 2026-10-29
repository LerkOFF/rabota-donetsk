"""Опрос VK и открытых баз: только новое, состояние, сбои источников, блокировки. Сеть подменена respx."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from rt_helpers import SHORT, ImageProvider

from rabota.collectors import avito, hh, trudvsem, vk
from rabota.models import RawPost
from rabota.realtime import pollers, state, vk_poller
from rabota.realtime.ingest import Ingestor

FIX = Path(__file__).resolve().parent.parent / "fixtures"
TOKEN = "vk-secret-token-0123456789"
VK_NOW = datetime.fromtimestamp(1793260000, UTC)


def load(rel: str) -> dict:
    return json.loads((FIX / rel).read_text(encoding="utf-8"))


WALLS = {
    "dnr.work": load("vk/wall_dnr_work.json"),
    "club68230454": load("vk/wall_club68230454.json"),
    "vakansiidnr": load("vk/wall_vakansiidnr.json"),
}
EMPTY = {"response": {"count": 0, "items": []}}


@pytest.fixture
def ing(db_path):
    return Ingestor(db_path, provider=ImageProvider())


def db_rows(db_path, sql, *args):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def st(db_path, key):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return state.get_state(c, key)
    finally:
        c.close()


# ---------------------------------------------------------------- VK

@pytest.fixture
def vk_env(monkeypatch):
    monkeypatch.setenv("VK_ACCESS_TOKEN", TOKEN)
    pauses: list[float] = []
    monkeypatch.setattr(vk, "_sleep", lambda s: None)
    monkeypatch.setattr(vk_poller, "_sleep", pauses.append)
    return pauses


def vk_api(handlers=None):
    handlers = dict(handlers or {})
    calls: list[dict[str, str]] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        params["_auth"] = request.headers.get("authorization", "")
        calls.append(params)
        answer = handlers.get(params["domain"], WALLS.get(params["domain"], EMPTY))
        return httpx.Response(200, json=answer)

    respx.post(vk.API_URL).mock(side_effect=side_effect)
    respx.get(url__regex=r"https://[^/]*userapi\.com/.*").mock(return_value=httpx.Response(404))  # картинки не нужны
    return handlers, calls


def vk_item(post_id, **kw):
    item = {"id": post_id, "owner_id": -100, "date": 1793259000, "text": SHORT.replace("0001", f"{post_id % 10000:04d}"),
            "marked_as_ads": 0}
    item.update(kw)
    return item


def vk_wall(*items):
    return {"response": {"count": len(items), "items": list(items)}}


@respx.mock
def test_vk_first_poll_ingests_and_sets_cursor(vk_env, ing, db_path):
    vk_api()
    totals = vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert totals["new"] >= 8 and totals["failed"] == 0
    ids = {r[0] for r in db_rows(db_path, "SELECT external_id FROM raw_posts WHERE source='dnr.work'")}
    assert ids == {"197969", "197968", "197962"}            # закреплённый 197964 не берётся
    assert st(db_path, "vk:dnr.work").last_id == 197969
    for src in vk.SOURCES:                                   # у каждого паблика своя запись, даже у пустых
        assert st(db_path, f"vk:{src.domain}").last_ok_at


@respx.mock
def test_vk_takes_only_newer_than_cursor(vk_env, ing, db_path):
    handlers, calls = vk_api()
    vk_poller.poll_vk_sync(ing, now=VK_NOW)
    again = vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert again["new"] == 0 and again["posts"] == 0
    handlers["dnr.work"] = vk_wall(vk_item(198000), *WALLS["dnr.work"]["response"]["items"])
    third = vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert third["new"] == 1
    assert st(db_path, "vk:dnr.work").last_id == 198000
    mine = [c for c in calls if c["domain"] == "dnr.work"]
    assert [c["count"] for c in mine] == [str(vk_poller.FIRST_COUNT), str(vk_poller.POLL_COUNT), str(vk_poller.POLL_COUNT)]


@respx.mock
def test_vk_pinned_post_does_not_move_cursor(vk_env, ing, db_path):
    vk_api({"dnr.work": vk_wall(vk_item(999999, is_pinned=1), vk_item(500))})
    vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert st(db_path, "vk:dnr.work").last_id == 500


@respx.mock
def test_vk_first_poll_drops_posts_older_than_ttl_but_moves_cursor(vk_env, ing, db_path):
    old = vk_item(77, date=1700000000)
    vk_api({s.domain: EMPTY for s in vk.SOURCES} | {"dnr.work": vk_wall(old)})
    totals = vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert totals["posts"] == 0
    assert st(db_path, "vk:dnr.work").last_id == 77


@respx.mock
def test_vk_broken_public_is_recorded_and_others_continue(vk_env, ing, db_path):
    vk_api({"club68230454": load("vk/error_15.json")})
    totals = vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert totals["failed"] == 1 and totals["new"] > 0
    bad = st(db_path, "vk:club68230454")
    assert bad.last_ok_at is None and "15" in bad.last_error
    assert TOKEN not in bad.last_error


@respx.mock
def test_vk_all_publics_failing_raises(vk_env, ing):
    vk_api({s.domain: load("vk/error_15.json") for s in vk.SOURCES})
    with pytest.raises(vk.VkError):
        vk_poller.poll_vk_sync(ing, now=VK_NOW)


def test_vk_without_token(monkeypatch, ing):
    assert vk_poller.is_configured() is False
    with pytest.raises(vk.VkTokenMissing):
        vk_poller.poll_vk_sync(ing)


@respx.mock
def test_vk_pauses_keep_under_three_requests_per_second(vk_env, ing):
    vk_api()
    vk_poller.poll_vk_sync(ing, now=VK_NOW)
    pauses = vk_env
    assert len(pauses) == len(vk.SOURCES) - 1
    assert all(0.34 < p <= 1.0 for p in pauses)


@respx.mock
def test_vk_token_goes_in_header_not_url_or_state(vk_env, ing, db_path):
    _, calls = vk_api()
    vk_poller.poll_vk_sync(ing, now=VK_NOW)
    assert all(c["_auth"] == f"Bearer {TOKEN}" for c in calls)
    assert all(TOKEN not in json.dumps(c) for c in [{k: v for k, v in c.items() if k != "_auth"} for c in calls])
    rows = db_rows(db_path, "SELECT * FROM ingest_state")
    assert all(TOKEN not in str(tuple(r)) for r in rows)


@respx.mock
def test_vk_async_wrapper(vk_env, ing):
    vk_api()
    totals = asyncio.run(vk_poller.poll_vk(ing, now=VK_NOW))
    assert totals["new"] > 0


def test_vk_interval_is_three_to_five_minutes():
    assert 180 <= vk_poller.VK_INTERVAL <= 300


# ---------------------------------------------------------------- открытые базы: таблица и общая логика

def test_pollers_table_matches_the_spec():
    got = {p.name: p.interval for p in pollers.POLLERS}
    assert got == {"trudvsem": 900, "hh": 1800, "mczn": 1800, "max": 1200, "avito": 3600}
    assert [p.name for p in pollers.POLLERS if p.allow_empty] == ["avito"]
    assert pollers.POLLERS[0].key == "poll:trudvsem"


def raw(i, source="x"):
    return RawPost("trudvsem", source, str(i), f"https://e.example/{i}", SHORT.replace("0001", f"{i:04d}"),
                   "2026-10-29T10:00:00+00:00")


def test_poll_ingests_new_and_marks_ok(ing, db_path):
    spec = pollers.PollerSpec("hh", "hh.ru", 1800, 50)
    seen = []

    def fetch(limit):
        seen.append(limit)
        return [raw(1), raw(2)]

    totals = pollers.poll_sync(spec, ing, fetch)
    assert seen == [50] and totals["new"] == 2
    assert st(db_path, "poll:hh").last_ok_at
    again = pollers.poll_sync(spec, ing, fetch)
    assert again["new"] == 0 and len(db_rows(db_path, "SELECT 1 FROM raw_posts")) == 2


def test_poll_empty_answer_is_not_success(ing, db_path):
    spec = pollers.PollerSpec("hh", "hh.ru", 1800, 50)
    pollers.poll_sync(spec, ing, lambda limit: [])
    s = st(db_path, "poll:hh")
    assert s.last_ok_at is None and "блокировка" in s.last_error


def test_poll_empty_answer_ok_for_avito(ing, db_path):
    spec = next(p for p in pollers.POLLERS if p.name == "avito")
    pollers.poll_sync(spec, ing, lambda limit: [])
    s = st(db_path, "poll:avito")
    assert s.last_ok_at and s.last_error is None


def test_poll_error_is_recorded_and_raised(ing, db_path):
    spec = pollers.PollerSpec("mczn", "Мой ЦЗН", 1800, 50)

    def boom(limit):
        raise httpx.ConnectError("нет сети")

    with pytest.raises(httpx.ConnectError):
        pollers.poll_sync(spec, ing, boom)
    assert "ConnectError" in st(db_path, "poll:mczn").last_error


def test_poll_good_run_after_error_clears_it(ing, db_path):
    spec = pollers.PollerSpec("mczn", "Мой ЦЗН", 1800, 50)
    with pytest.raises(RuntimeError):
        pollers.poll_sync(spec, ing, lambda limit: (_ for _ in ()).throw(RuntimeError("сбой")))
    pollers.poll_sync(spec, ing, lambda limit: [raw(5)])
    assert st(db_path, "poll:mczn").last_error is None


def test_poll_uses_collector_fetch_by_name(ing, monkeypatch):
    names = []
    monkeypatch.setattr(pollers, "load_fetch", lambda name: (names.append(name), lambda limit: [])[1])
    for spec in pollers.POLLERS:
        pollers.poll_sync(spec, ing)
    assert names == ["trudvsem", "hh", "mczn", "max", "avito"]


def test_load_fetch_returns_real_collectors():
    assert pollers.load_fetch("trudvsem") is trudvsem.fetch
    assert pollers.load_fetch("avito") is avito.fetch


def test_poll_async_wrapper(ing):
    spec = pollers.PollerSpec("hh", "hh.ru", 1800, 50)
    totals = asyncio.run(pollers.poll(spec, ing, lambda limit: [raw(9)]))
    assert totals["new"] == 1


# ---------------------------------------------------------------- открытые базы: настоящие сборщики на фикстурах

@respx.mock
def test_trudvsem_end_to_end(ing, db_path, monkeypatch):
    monkeypatch.setattr(trudvsem.time, "sleep", lambda s: None)
    respx.get(trudvsem.API_URL.format(region=trudvsem.REGION_DNR)).mock(
        return_value=httpx.Response(200, json=load("trudvsem/region_dnr.json")))
    spec = pollers.POLLERS[0]
    first = pollers.poll_sync(spec, ing)
    assert first["new"] > 0 and st(db_path, "poll:trudvsem").last_ok_at
    assert pollers.poll_sync(spec, ing)["new"] == 0


@respx.mock
def test_trudvsem_down_is_not_green(ing, db_path, monkeypatch):
    monkeypatch.setattr(trudvsem.time, "sleep", lambda s: None)
    respx.get(trudvsem.API_URL.format(region=trudvsem.REGION_DNR)).mock(return_value=httpx.Response(503))
    pollers.poll_sync(pollers.POLLERS[0], ing)
    assert st(db_path, "poll:trudvsem").last_ok_at is None


@respx.mock
def test_avito_blocked_is_normal_and_not_bypassed(ing, db_path, monkeypatch):
    monkeypatch.setattr(avito.time, "sleep", lambda s: None)
    route = respx.route(host="www.avito.ru").mock(return_value=httpx.Response(429, text="captcha"))
    spec = next(p for p in pollers.POLLERS if p.name == "avito")
    totals = pollers.poll_sync(spec, ing)
    assert totals["new"] == 0 and route.call_count == 1      # один запрос и стоп, никаких обходов
    assert st(db_path, "poll:avito").last_ok_at


@respx.mock
def test_hh_blocked_shows_as_problem(ing, db_path, monkeypatch):
    monkeypatch.setattr(hh.time, "sleep", lambda s: None)
    respx.route(host="hh.ru").mock(return_value=httpx.Response(403, text="captcha"))
    pollers.poll_sync(next(p for p in pollers.POLLERS if p.name == "hh"), ing)
    assert st(db_path, "poll:hh").last_ok_at is None
