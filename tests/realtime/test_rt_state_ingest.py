"""Состояние приёма и приём одного поста: идемпотентность, дубли, срок жизни, картинки."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from rt_helpers import FULL, SHORT, ImageProvider, make_jpeg, run

from rabota.db import connect
from rabota.models import RawPost
from rabota.realtime import ingest as ingest_mod
from rabota.realtime import state
from rabota.realtime.ingest import Ingestor, ingest_post, open_db

NOW = datetime.now(UTC)


def iso(days_ago: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def post(ext="1", source="chan_a", text=SHORT, days_ago=0.0, images=None, stype="telegram") -> RawPost:
    return RawPost(stype, source, str(ext), f"https://t.me/{source}/{ext}", text, iso(days_ago), images or [])


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------- состояние

def test_ingest_state_table_exists(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(ingest_state)")]
    assert cols == ["source", "last_id", "updated_at", "last_ok_at", "last_error"]


def test_source_key_lowercases():
    assert state.source_key("telegram", " Rabota_DonetskV ") == "telegram:rabota_donetskv"


def test_mark_ok_creates_and_updates(conn):
    assert state.get_state(conn, "vk:a") is None
    state.mark_ok(conn, "vk:a", 10, NOW)
    st = state.get_state(conn, "vk:a")
    assert st.last_id == 10 and st.last_ok_at == st.updated_at and st.last_error is None


def test_last_id_never_goes_back(conn):
    state.mark_ok(conn, "vk:a", 10)
    state.mark_ok(conn, "vk:a", 7)
    assert state.get_last_id(conn, "vk:a") == 10
    state.mark_ok(conn, "vk:a", None)
    assert state.get_last_id(conn, "vk:a") == 10
    state.mark_ok(conn, "vk:a", 12)
    assert state.get_last_id(conn, "vk:a") == 12


def test_mark_error_keeps_last_ok_and_id(conn):
    state.mark_ok(conn, "vk:a", 5, NOW)
    state.mark_error(conn, "vk:a", "сеть\nпропала  " + "x" * 500, NOW + timedelta(minutes=1))
    st = state.get_state(conn, "vk:a")
    assert st.last_id == 5 and st.last_ok_at == state.now_iso(NOW)
    assert st.updated_at == state.now_iso(NOW + timedelta(minutes=1))
    assert "\n" not in st.last_error and len(st.last_error) <= state.ERROR_MAX_CHARS


def test_mark_ok_clears_error(conn):
    state.mark_error(conn, "poll:hh", "сбой")
    assert state.get_state(conn, "poll:hh").last_ok_at is None
    state.mark_ok(conn, "poll:hh")
    assert state.get_state(conn, "poll:hh").last_error is None


def test_mark_empty_is_not_success(conn):
    state.mark_empty(conn, "poll:hh", "пусто")
    st = state.get_state(conn, "poll:hh")
    assert st.last_ok_at is None and st.last_error == "пусто"


def test_all_states_sorted(conn):
    state.mark_ok(conn, "b:1")
    state.mark_ok(conn, "a:1")
    assert [s.source for s in state.all_states(conn)] == ["a:1", "b:1"]


# ---------------------------------------------------------------- ingest_post

def test_new_post_is_saved_and_parsed(conn):
    res = ingest_post(conn, post())
    assert res["new"] is True and res["kind"] == "vacancy" and res["vacancies"] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == 1
    v = conn.execute("SELECT * FROM vacancies").fetchone()
    assert v["profession"] == "бариста" and v["source"] == "chan_a" and v["external_id"] == "1"
    assert v["status"] == "active" and v["last_seen_at"]


def test_ingest_is_idempotent(conn):
    ingest_post(conn, post())
    again = ingest_post(conn, post())
    assert again["new"] is False and again["vacancies"] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_same_external_id_other_source_is_different_post(conn):
    ingest_post(conn, post("1", "chan_a"))
    assert ingest_post(conn, post("1", "chan_b"))["new"] is True


def test_non_vacancy_saved_raw_only(conn):
    res = ingest_post(conn, post(text="Реклама! Заходите на наш сайт, скидки 50%. Подписывайтесь на канал."))
    assert res["new"] is True and res["vacancies"] == 0 and res["kind"] != "vacancy"
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0
    assert ingest_post(conn, post(text="Реклама! Заходите на наш сайт, скидки 50%. Подписывайтесь на канал."))["new"] is False


def test_only_the_new_post_is_analyzed(conn, monkeypatch):
    seen = []
    real = ingest_mod.analyze
    monkeypatch.setattr(ingest_mod, "analyze", lambda raw: (seen.append(raw.external_id), real(raw))[1])
    ingest_post(conn, post("1"))
    ingest_post(conn, post("2", text=SHORT.replace("0001", "0002")))
    ingest_post(conn, post("1"))
    assert seen == ["1", "2"]


def test_repost_in_other_channel_is_merged_and_extends_life(conn):
    first = ingest_post(conn, post("1", "chan_a", days_ago=15))
    assert first["duplicates"] == 0
    second = ingest_post(conn, post("7", "chan_b", days_ago=0))
    assert second["duplicates"] == 1
    main = conn.execute("SELECT * FROM vacancies WHERE duplicate_of IS NULL").fetchone()
    dup = conn.execute("SELECT * FROM vacancies WHERE duplicate_of IS NOT NULL").fetchone()
    assert main["external_id"] == "1" and dup["external_id"] == "7"
    assert main["last_seen_at"] == iso(0)          # срок жизни пошёл заново
    assert main["published_at"] == iso(15)


def test_repost_revives_expired_vacancy(conn):
    ingest_post(conn, post("1", "chan_a", days_ago=30))
    assert conn.execute("SELECT status FROM vacancies").fetchone()[0] == "expired"
    ingest_post(conn, post("9", "chan_b", days_ago=0))
    row = conn.execute("SELECT status FROM vacancies WHERE duplicate_of IS NULL").fetchone()
    assert row["status"] == "active"


def test_run_dedupe_false_defers_merge(conn):
    ingest_post(conn, post("1", "chan_a", days_ago=2), run_dedupe=False)
    res = ingest_post(conn, post("2", "chan_b", days_ago=1), run_dedupe=False)
    assert res["duplicates"] == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE duplicate_of IS NOT NULL").fetchone()[0] == 0


# ---------------------------------------------------------------- картинки

def poster_post(tmp_path, ext="5", text="", seed=3) -> RawPost:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    f = media / f"p{seed}.jpg"
    f.write_bytes(make_jpeg(seed))
    return post(ext, text=text, images=[f.as_uri()])


def test_vacancy_from_image_when_text_is_empty(conn, tmp_path):
    prov = ImageProvider()
    res = ingest_post(conn, poster_post(tmp_path), prov)
    assert res["new"] is True and res["from_images"] == 1 and res["vacancies"] == 1 and res["kind"] == "vacancy"
    v = conn.execute("SELECT * FROM vacancies").fetchone()
    assert v["profession"] == "Бариста" and v["external_id"] == "5"
    assert prov.calls == 1


def test_image_not_used_when_text_is_complete(conn, tmp_path):
    prov = ImageProvider()
    raw = poster_post(tmp_path, text=FULL)
    res = ingest_post(conn, raw, prov)
    assert res["from_images"] == 0 and prov.calls == 0 and res["vacancies"] >= 1


def test_image_profession_same_as_text_is_not_duplicated(conn, tmp_path):
    prov = ImageProvider({"kind": "vacancy", "vacancies": [{"profession": "бариста", "phones": []}], "raw_text": ""})
    res = ingest_post(conn, poster_post(tmp_path, text=SHORT), prov)
    assert res["from_images"] == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_image_failure_does_not_break_text(conn, tmp_path):
    class Boom(ImageProvider):
        def extract(self, image_bytes, hint_text=""):
            raise RuntimeError("модель недоступна")

    res = ingest_post(conn, poster_post(tmp_path, text=SHORT), Boom())
    assert res["new"] is True and res["vacancies"] == 1 and res["from_images"] == 0


def test_unreadable_image_file_is_skipped(conn, tmp_path):
    raw = post("8", text=SHORT, images=[(tmp_path / "media" / "nope.jpg").as_uri()])
    res = ingest_post(conn, raw, ImageProvider())
    assert res["new"] is True and res["from_images"] == 0


def test_default_provider_without_keys_is_sidecar():
    from rabota.vision.providers import SidecarProvider

    assert isinstance(ingest_mod.default_provider(), SidecarProvider)


def test_default_provider_with_key_uses_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "null")
    from rabota.vision.providers import NullProvider

    assert isinstance(ingest_mod.default_provider(), NullProvider)


# ---------------------------------------------------------------- Ingestor

def test_ingestor_batch_dedupes_once_and_counts(db_path):
    ing = Ingestor(db_path, provider=ImageProvider())
    totals = ing.ingest_many([post("1", "chan_a", days_ago=3), post("2", "chan_b", days_ago=2), post("1", "chan_a", days_ago=3)])
    assert totals["posts"] == 3 and totals["new"] == 2 and totals["vacancies"] == 2 and totals["duplicates"] == 1
    assert totals["errors"] == 0


def test_ingestor_survives_bad_post(db_path, monkeypatch):
    real = ingest_mod.analyze

    def flaky(raw):
        if raw.external_id == "2":
            raise ValueError("сломанный пост")
        return real(raw)

    monkeypatch.setattr(ingest_mod, "analyze", flaky)
    ing = Ingestor(db_path)
    totals = ing.ingest_many([post("1", text=SHORT), post("2", text=SHORT.replace("0001", "0002")),
                              post("3", text=SHORT.replace("0001", "0003"))])
    assert totals["errors"] == 1 and totals["new"] == 2


def test_ingestor_raises_database_errors(db_path):
    def broken(path):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        Ingestor(db_path, opener=broken).ingest_many([post()])


def test_ingestor_async_and_state_helpers(db_path):
    ing = Ingestor(db_path)

    async def go():
        totals = await ing.aingest_many([post("1")])
        await ing.acall(state.mark_ok, "telegram:chan_a", 42)
        return totals, await ing.acall(state.get_last_id, "telegram:chan_a")

    totals, last = run(go())
    assert totals["new"] == 1 and last == 42


def test_open_db_uses_wal_for_files(db_path):
    c = open_db(db_path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == ingest_mod.BUSY_TIMEOUT_MS
    c.close()


def test_add_totals():
    assert ingest_mod.add_totals({"a": 1}, {"a": 2, "b": 3}) == {"a": 3, "b": 3}
