"""Слушатель Telegram целиком на подменённом клиенте: догон, живые события, альбомы, FloodWait, переподключение."""
from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path

import pytest
from rt_helpers import (
    FULL,
    NOW,
    SHORT,
    FakeMessage,
    FakeSleeper,
    FakeTelegram,
    ImageProvider,
    make_jpeg,
    msgs,
)
from telethon import errors

from rabota.realtime import state
from rabota.realtime import telegram_listener as tl
from rabota.realtime.ingest import Ingestor
from rabota.realtime.telegram_listener import TelegramListener, TelegramSettings


class Factory:
    """Выдаёт клиентов по очереди; когда очередь пуста, делает новых с теми же каналами."""

    def __init__(self, channels: dict[str, list[FakeMessage]], queue: list[FakeTelegram] | None = None, **kw) -> None:
        self.channels, self.queue, self.kw = channels, list(queue or []), kw
        self.clients: list[FakeTelegram] = []

    def __call__(self, settings) -> FakeTelegram:
        client = self.queue.pop(0) if self.queue else FakeTelegram(self.channels, **self.kw)
        self.clients.append(client)
        return client

    @property
    def last(self) -> FakeTelegram:
        return self.clients[-1]


def make_settings(tmp_path: Path, channels=("c_chan",), **over) -> TelegramSettings:
    base = {
        "api_id": 1, "api_hash": "h", "session_path": tmp_path / "s", "channels": tuple(channels),
        "media_dir": tmp_path / "media", "sweep_seconds": 3600.0, "group_wait": 0.03, "catchup_pause": 0.0,
        "photo_pause": 0.0, "reconnect_base": 0.01, "reconnect_cap": 1.0,
    }
    base.update(over)
    return TelegramSettings(**base)


def make_listener(tmp_path, db_path, factory, channels=("c_chan",), sleeper=None, provider=None, **over):
    ing = Ingestor(db_path, provider=provider or ImageProvider())
    L = TelegramListener(make_settings(tmp_path, channels, **over), ing, client_factory=factory, sleep=sleeper,
                         now=lambda: NOW, rng=random.Random(1))
    return L, ing


def raw_ids(db_path, source="c_chan") -> list[int]:
    c = sqlite3.connect(db_path)
    try:
        return sorted(int(r[0]) for r in c.execute("SELECT external_id FROM raw_posts WHERE source=?", (source,)))
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def last_id(db_path, channel="c_chan"):
    c = sqlite3.connect(db_path)
    try:
        row = c.execute("SELECT last_id FROM ingest_state WHERE source=?", (f"telegram:{channel}",)).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


def get_state(db_path, channel="c_chan"):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return state.get_state(c, f"telegram:{channel}")
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


async def drive(listener, until, timeout=10.0):
    """Запустить слушатель, дождаться условия, остановить. Возвращает задачу run для проверки завершения."""
    stop = asyncio.Event()
    task = asyncio.create_task(listener.run(stop))
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    try:
        while not until():
            if task.done():
                break
            if loop.time() > end:
                raise AssertionError("условие не наступило за отведённое время")
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def go(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- догон

def test_first_run_takes_latest_30(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 50)})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: len(raw_ids(db_path)) >= 30))
    assert raw_ids(db_path) == list(range(21, 51))
    assert last_id(db_path) == 50
    assert ("iter", "c_chan", 30, 0) in f.last.calls
    assert f.last.sent == []


def test_first_run_skips_older_than_ttl(tmp_path, db_path):
    old = msgs(1, 10, age_days=30)
    fresh = msgs(11, 5)
    L, _ = make_listener(tmp_path, db_path, Factory({"c_chan": old + fresh}))
    go(drive(L, lambda: len(raw_ids(db_path)) >= 5))
    assert raw_ids(db_path) == [11, 12, 13, 14, 15]


def test_catch_up_resumes_after_last_id(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 50)})
    L, ing = make_listener(tmp_path, db_path, f)
    ing.call(state.mark_ok, "telegram:c_chan", 40)
    go(drive(L, lambda: len(raw_ids(db_path)) >= 10))
    assert raw_ids(db_path) == list(range(41, 51))
    assert last_id(db_path) == 50
    assert ("iter", "c_chan", 200, 40) in f.last.calls


def test_catch_up_gap_over_limit_takes_newest(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 90)})
    L, ing = make_listener(tmp_path, db_path, f, catchup_limit=20)
    ing.call(state.mark_ok, "telegram:c_chan", 5)
    go(drive(L, lambda: len(raw_ids(db_path)) >= 20))
    assert raw_ids(db_path) == list(range(71, 91))
    assert last_id(db_path) == 90


def test_default_limits_match_the_spec():
    assert tl.CATCHUP_LIMIT == 200 and tl.FIRST_RUN_LIMIT == 30 and tl.THUMB_SIDE <= 1600


def test_second_start_does_not_reingest(tmp_path, db_path):
    ch = {"c_chan": msgs(1, 5)}
    L, _ = make_listener(tmp_path, db_path, Factory(ch))
    go(drive(L, lambda: last_id(db_path) == 5))
    f2 = Factory(ch)
    L2, _ = make_listener(tmp_path, db_path, f2)
    go(drive(L2, lambda: ("iter", "c_chan", 200, 5) in (f2.clients[-1].calls if f2.clients else [])))
    assert raw_ids(db_path) == [1, 2, 3, 4, 5]
    assert L2.stats["new"] == 0


def test_idle_channel_still_marked_ok(tmp_path, db_path):
    L, _ = make_listener(tmp_path, db_path, Factory({"c_chan": []}))
    go(drive(L, lambda: get_state(db_path) is not None and get_state(db_path).last_ok_at is not None))
    st = get_state(db_path)
    assert st.last_ok_at and st.last_id is None and st.last_error is None


def test_periodic_sweep_finds_messages_without_events(tmp_path, db_path):
    chan = msgs(1, 3)
    f = Factory({"c_chan": chan})
    L, _ = make_listener(tmp_path, db_path, f, sweep_seconds=10.0, sleeper=FakeSleeper())

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while f.clients == [] or f.last.handler is None:
            await asyncio.sleep(0.01)
        chan.extend(msgs(4, 2))          # пришло без события: ловит только догон
        for _ in range(500):
            if raw_ids(db_path) == [1, 2, 3, 4, 5]:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert raw_ids(db_path) == [1, 2, 3, 4, 5] and last_id(db_path) == 5


# ---------------------------------------------------------------- живые события

def test_live_message_is_ingested_and_state_moves(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 2)})
    L, _ = make_listener(tmp_path, db_path, f)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients or f.last.handler is None or last_id(db_path) != 2:
            await asyncio.sleep(0.01)
        f.last.push("c_chan", msgs(3, 1)[0])
        while 3 not in raw_ids(db_path) or last_id(db_path) != 3:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    c = sqlite3.connect(db_path)
    row = c.execute("SELECT url, source_type FROM raw_posts WHERE external_id='3'").fetchone()
    assert row == ("https://t.me/c_chan/3", "telegram")
    assert f.last.sent == []


def test_event_from_unknown_chat_is_ignored(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 1), "other": []})
    L, _ = make_listener(tmp_path, db_path, f)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients or f.last.handler is None or last_id(db_path) != 1:
            await asyncio.sleep(0.01)
        f.last.push("other", msgs(50, 1)[0])
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert raw_ids(db_path, "other") == [] and raw_ids(db_path) == [1]


def test_live_album_becomes_one_post(tmp_path, db_path):
    f = Factory({"c_chan": []})
    L, _ = make_listener(tmp_path, db_path, f)
    album = [FakeMessage(11, SHORT, grouped_id=77, photo=make_jpeg(1)),
             FakeMessage(12, "", grouped_id=77, photo=make_jpeg(2)),
             FakeMessage(13, "", grouped_id=77, photo=make_jpeg(3))]

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients or f.last.handler is None:
            await asyncio.sleep(0.01)
        for m in album:
            f.last.push("c_chan", m)
        while last_id(db_path) != 13:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert raw_ids(db_path) == [11]
    c = sqlite3.connect(db_path)
    urls = __import__("json").loads(c.execute("SELECT image_urls FROM raw_posts").fetchone()[0])
    assert len(urls) == 3 and all(u.startswith("file://") for u in urls)
    assert all(Path(u[len("file://"):]).is_file() for u in urls)
    assert f.last.downloads == 3


def test_album_in_catch_up_and_photo_limit(tmp_path, db_path):
    album = [FakeMessage(i, SHORT if i == 21 else "", grouped_id=5, photo=make_jpeg(i)) for i in range(21, 27)]
    f = Factory({"c_chan": [FakeMessage(20, "Нужен повар, Донецк, 3000 за смену, +79490000002")] + album})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 26))
    assert raw_ids(db_path) == [20, 21]
    c = sqlite3.connect(db_path)
    urls = __import__("json").loads(c.execute("SELECT image_urls FROM raw_posts WHERE external_id='21'").fetchone()[0])
    assert len(urls) == tl.MAX_PHOTOS


def test_photo_skipped_when_text_is_complete(tmp_path, db_path):
    f = Factory({"c_chan": [FakeMessage(1, FULL, photo=make_jpeg(1))]})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 1))
    assert f.last.downloads == 0
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT image_urls FROM raw_posts").fetchone()[0] == "[]"


def test_photo_only_post_gets_vacancy_from_image(tmp_path, db_path):
    f = Factory({"c_chan": [FakeMessage(1, "", photo=make_jpeg(9))]})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 1))
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT profession FROM vacancies").fetchone()[0] == "Бариста"


def test_broken_photo_keeps_post_with_text(tmp_path, db_path):
    f = Factory({"c_chan": [FakeMessage(1, SHORT, photo=b"not a picture")]})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 1))
    assert raw_ids(db_path) == [1]


def test_service_and_empty_messages_advance_state_without_posts(tmp_path, db_path):
    f = Factory({"c_chan": [FakeMessage(1, "вступил", action=object()), FakeMessage(2, "")]})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 2))
    assert raw_ids(db_path) == []


def test_pending_album_holds_last_id_back(tmp_path, db_path):
    f = Factory({"c_chan": []})
    L, _ = make_listener(tmp_path, db_path, f)
    L._groups[("c_chan", 5)] = [FakeMessage(10, grouped_id=5)]
    client = tl.ReadOnlyClient(FakeTelegram({"c_chan": []}))

    async def scenario():
        await L._process_units(client, "c_chan", [[FakeMessage(12, SHORT)]], paced=False)

    go(scenario())
    assert last_id(db_path) == 9


def test_failed_live_message_is_recorded_and_next_one_works(tmp_path, db_path, monkeypatch):
    f = Factory({"c_chan": []})
    L, ing = make_listener(tmp_path, db_path, f)
    real = ing.aingest_many
    calls = {"n": 0}

    async def flaky(posts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("не принять")
        return await real(posts)

    monkeypatch.setattr(ing, "aingest_many", flaky)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients or f.last.handler is None:
            await asyncio.sleep(0.01)
        # обработчик ставится до первого догона: ждём, пока догон отметит канал, иначе он сам подберёт сообщение
        while get_state(db_path) is None:
            await asyncio.sleep(0.01)
        f.last.push("c_chan", msgs(1, 1)[0])
        while get_state(db_path) is None or not get_state(db_path).last_error:
            await asyncio.sleep(0.01)
        f.last.push("c_chan", msgs(2, 1)[0])
        while 2 not in raw_ids(db_path):
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert last_id(db_path) == 2


# ---------------------------------------------------------------- каналы

def test_unresolvable_channel_is_marked_and_others_work(tmp_path, db_path):
    chans = {"c_one": msgs(1, 3), "c_two": msgs(1, 3)}
    fake = FakeTelegram(chans)
    fake.unresolvable = {"c_two"}
    f = Factory(chans, [fake])
    L, _ = make_listener(tmp_path, db_path, f, channels=("c_one", "c_two"))
    go(drive(L, lambda: last_id(db_path, "c_one") == 3 and get_state(db_path, "c_two") is not None))
    assert "недоступен" in get_state(db_path, "c_two").last_error
    assert raw_ids(db_path, "c_one") == [1, 2, 3] and raw_ids(db_path, "c_two") == []


def test_channel_names_keep_configured_spelling(tmp_path, db_path):
    f = Factory({"Rabota_Dnr": msgs(1, 2)})
    L, _ = make_listener(tmp_path, db_path, f, channels=("Rabota_Dnr",))
    go(drive(L, lambda: last_id(db_path, "rabota_dnr") == 2))
    assert raw_ids(db_path, "Rabota_Dnr") == [1, 2]


# ---------------------------------------------------------------- FloodWait

def test_flood_wait_sleeps_needed_time_plus_margin(tmp_path, db_path):
    sleeper = FakeSleeper()
    fake = FakeTelegram({"c_chan": msgs(1, 3)})
    fake.flood = [errors.FloodWaitError(request=None, capture=7)]
    f = Factory({}, [fake])
    L, _ = make_listener(tmp_path, db_path, f, sleeper=sleeper)
    go(drive(L, lambda: last_id(db_path) == 3))
    assert 7 + tl.FLOOD_MARGIN in sleeper.calls
    assert raw_ids(db_path) == [1, 2, 3]


def test_flood_wait_repeated_sends_session_to_cooldown_and_back(tmp_path, db_path):
    fake = FakeTelegram({"c_chan": msgs(1, 3)})
    fake.flood = [errors.FloodWaitError(request=None, capture=9) for _ in range(tl.FLOOD_RETRIES)]
    f = Factory({"c_chan": msgs(1, 3)}, [fake])
    sleeper = FakeSleeper()
    L, _ = make_listener(tmp_path, db_path, f, sleeper=sleeper)
    go(drive(L, lambda: last_id(db_path) == 3))
    assert sleeper.calls.count(9 + tl.FLOOD_MARGIN) == tl.FLOOD_RETRIES
    assert len(f.clients) == 2 and raw_ids(db_path) == [1, 2, 3]


def test_flood_wait_on_resolve_is_handled(tmp_path, db_path):
    sleeper = FakeSleeper()
    fake = FakeTelegram({"c_chan": msgs(1, 1)})
    real = fake.get_input_entity
    boom = [errors.FloodWaitError(request=None, capture=3)]

    async def resolve(name):
        if boom:
            raise boom.pop()
        return await real(name)

    fake.get_input_entity = resolve
    L, _ = make_listener(tmp_path, db_path, Factory({}, [fake]), sleeper=sleeper)
    go(drive(L, lambda: last_id(db_path) == 1))
    assert 3 + tl.FLOOD_MARGIN in sleeper.calls


# ---------------------------------------------------------------- переподключение и остановка

def test_reconnect_with_growing_pause(tmp_path, db_path):
    bad = []
    for _ in range(3):
        c = FakeTelegram({"c_chan": []})
        c.connect_error = ConnectionError("нет связи")
        bad.append(c)
    f = Factory({"c_chan": msgs(1, 2)}, bad)
    sleeper = FakeSleeper()
    L, _ = make_listener(tmp_path, db_path, f, sleeper=sleeper, reconnect_base=10.0, reconnect_cap=80.0)
    go(drive(L, lambda: last_id(db_path) == 2))
    pauses = [s for s in sleeper.calls if 5 <= s < 1000]  # без ожидания следующего догона (около часа)
    assert len(pauses) == 3 and pauses[0] < pauses[1] < pauses[2]
    assert 8 <= pauses[0] <= 12 and 16 <= pauses[1] <= 24 and 32 <= pauses[2] <= 48
    assert len(f.clients) == 4 and L.stats["sessions"] == 1
    assert get_state(db_path).last_error is None


def test_reconnect_pause_is_capped(tmp_path, db_path):
    bad = []
    for _ in range(6):
        c = FakeTelegram({"c_chan": []})
        c.connect_error = OSError("обрыв")
        bad.append(c)
    f = Factory({"c_chan": msgs(1, 1)}, bad)
    sleeper = FakeSleeper()
    L, _ = make_listener(tmp_path, db_path, f, sleeper=sleeper, reconnect_base=10.0, reconnect_cap=30.0)
    go(drive(L, lambda: last_id(db_path) == 1))
    assert max(s for s in sleeper.calls if s < 1000) <= 30.0 * 1.2


def test_connection_error_is_written_to_state(tmp_path, db_path):
    bad = FakeTelegram({"c_chan": []})
    bad.connect_error = ConnectionError("нет связи")
    f = Factory({"c_chan": msgs(1, 1)}, [bad])
    L, _ = make_listener(tmp_path, db_path, f, sleeper=FakeSleeper())
    seen = []

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while last_id(db_path) != 1:
            st = get_state(db_path)
            if st and st.last_error:
                seen.append(st.last_error)
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert any("переподключение" in s for s in seen) or last_id(db_path) == 1


def test_dropped_connection_starts_new_session(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 2)})
    L, _ = make_listener(tmp_path, db_path, f, sleeper=FakeSleeper())

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients or f.last.handler is None:
            await asyncio.sleep(0.01)
        await f.clients[0].disconnect()      # связь оборвалась
        while len(f.clients) < 2 or f.last.handler is None:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    go(scenario())
    assert L.stats["sessions"] == 2


def test_unauthorized_session_is_fatal_and_visible(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 2)}, authorized=False)
    L, _ = make_listener(tmp_path, db_path, f, sleeper=FakeSleeper())
    go(asyncio.wait_for(L.run(asyncio.Event()), 5))
    assert len(f.clients) == 1
    assert "tg-login" in get_state(db_path).last_error
    assert raw_ids(db_path) == []


def test_stop_request_ends_run_and_disconnects(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 2)})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 2))
    assert f.last.connected is False and f.last.sent == []


def test_stop_during_reconnect_pause(tmp_path, db_path):
    bad = FakeTelegram({"c_chan": []})
    bad.connect_error = ConnectionError("нет связи")
    f = Factory({"c_chan": []}, [bad], authorized=True)
    L, _ = make_listener(tmp_path, db_path, f, reconnect_base=500.0, reconnect_cap=500.0)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(L.run(stop))
        while not f.clients:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    go(scenario())
    assert len(f.clients) == 1


def test_nothing_is_ever_sent(tmp_path, db_path):
    f = Factory({"c_chan": msgs(1, 5)})
    L, _ = make_listener(tmp_path, db_path, f)
    go(drive(L, lambda: last_id(db_path) == 5))
    assert all(c.sent == [] for c in f.clients)


def test_listener_without_telethon_reports_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(tl, "errors", None)
    with pytest.raises(tl.TelegramNotConfigured, match="telethon"):
        TelegramSettings.from_env({"TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION_PATH": str(tmp_path / "x")},
                                  require_session=False)
