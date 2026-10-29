"""Срок жизни вакансии: 21 день с последней публикации, повтор обнуляет отсчёт."""
from datetime import UTC, datetime, timedelta

import pytest

from rabota import lifecycle
from rabota.db import connect, save_vacancy
from rabota.models import Vacancy
from rabota.pipeline.dedupe import assign_duplicates, dedupe

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def _vac(days_ago: float, source="telegram", chan="a", ext="1", phone="+79490000001", profession="бариста", **kw):
    return Vacancy(
        profession=profession, source_type=source, source=chan, external_id=ext, source_url=f"https://x/{chan}/{ext}",
        published_at=_iso(days_ago), contacts={"phones": [phone]}, city="Донецк", **kw,
    )


@pytest.fixture
def conn():
    return connect(":memory:")


def _status(conn, vid):
    return conn.execute("SELECT status, last_seen_at, duplicate_of FROM vacancies WHERE id=?", (vid,)).fetchone()


def test_parse_and_canonical_iso():
    assert lifecycle.to_utc_iso("2026-10-29T15:00:00+03:00") == "2026-10-29T12:00:00+00:00"
    assert lifecycle.to_utc_iso("2026-10-29 12:00:00") == "2026-10-29T12:00:00+00:00"
    assert lifecycle.to_utc_iso("2026-10-29T12:00:00Z") == "2026-10-29T12:00:00+00:00"
    assert lifecycle.to_utc_iso("не дата") is None and lifecycle.to_utc_iso(None) is None


def test_is_stale_boundary():
    assert lifecycle.is_stale(_iso(21.1), 21, NOW) is True
    assert lifecycle.is_stale(_iso(20.9), 21, NOW) is False
    assert lifecycle.is_stale(None, 21, NOW) is False


def test_expire_hides_only_old(conn):
    old = save_vacancy(conn, _vac(30, ext="1"))
    # save_vacancy сам скрывает пост старше TTL по реальному времени, поэтому для проверки expire выставим active вручную
    conn.execute("UPDATE vacancies SET status='active' WHERE id=?", (old,))
    fresh = save_vacancy(conn, _vac(2, ext="2", profession="повар"))
    assert lifecycle.expire(conn, 21, NOW) == 1
    assert _status(conn, old)["status"] == "expired"
    assert _status(conn, fresh)["status"] == "active"
    assert lifecycle.expire(conn, 21, NOW) == 0  # повторный запуск ничего не меняет


def test_old_post_is_saved_expired(conn):
    vid = save_vacancy(conn, Vacancy(profession="повар", published_at="2020-01-01T00:00:00+00:00"))
    assert _status(conn, vid)["status"] == "expired"


def test_last_seen_defaults_to_published(conn):
    vid = save_vacancy(conn, _vac(2))
    assert _status(conn, vid)["last_seen_at"] == _iso(2)


def test_repost_resets_the_clock(conn):
    """Тот же работодатель выложил ту же вакансию в другом канале: отсчёт идёт заново."""
    first = save_vacancy(conn, _vac(20, chan="a", ext="1"))
    repost = save_vacancy(conn, _vac(1, chan="b", ext="9"))
    dedupe(conn)
    main = _status(conn, first)
    assert main["last_seen_at"] == _iso(1)          # отсчёт от повторной публикации
    assert main["status"] == "active"
    assert _status(conn, repost)["duplicate_of"] == first
    assert lifecycle.expire(conn, 21, NOW) == 0
    # через 21 день после повтора вакансия всё же исчезает (вместе со своим дублем)
    assert lifecycle.expire(conn, 21, NOW + timedelta(days=21)) == 2
    assert _status(conn, first)["status"] == "expired"


def test_expired_vacancy_is_revived_by_new_post(conn):
    old = save_vacancy(conn, _vac(40, chan="a", ext="1"))
    assert _status(conn, old)["status"] == "expired"
    new = save_vacancy(conn, _vac(0.5, chan="a", ext="2"))
    dedupe(conn)
    row = _status(conn, old)
    assert row["status"] == "active" and row["last_seen_at"] == _iso(0.5)
    assert _status(conn, new)["duplicate_of"] == old


def test_other_person_does_not_reset(conn):
    a = save_vacancy(conn, _vac(20, ext="1", phone="+79490000001"))
    save_vacancy(conn, _vac(1, ext="2", phone="+79490000002", chan="b"))
    dedupe(conn)
    assert _status(conn, a)["last_seen_at"] == _iso(20)


def test_other_profession_does_not_reset(conn):
    a = save_vacancy(conn, _vac(20, ext="1"))
    save_vacancy(conn, _vac(1, ext="2", profession="повар"))
    dedupe(conn)
    assert _status(conn, a)["last_seen_at"] == _iso(20)


def test_dedupe_is_idempotent_for_last_seen(conn):
    first = save_vacancy(conn, _vac(20, chan="a", ext="1"))
    save_vacancy(conn, _vac(1, chan="b", ext="9"))
    dedupe(conn)
    dedupe(conn)
    assert _status(conn, first)["last_seen_at"] == _iso(1)


def test_in_memory_batch_updates_last_seen():
    a, b = _vac(20, chan="a", ext="1"), _vac(1, chan="b", ext="2")
    assign_duplicates([a, b])
    assert a.duplicate_of is None and a.last_seen_at == _iso(1)


def test_purge_removes_long_expired_with_duplicates_and_raw(conn):
    conn.execute(
        "INSERT INTO raw_posts(source_type,source,external_id,url,text) VALUES ('telegram','a','1','u','t')"
    )
    main = save_vacancy(conn, _vac(100, chan="a", ext="1"))
    dup = save_vacancy(conn, _vac(100, chan="b", ext="2"))
    conn.execute("UPDATE vacancies SET duplicate_of=? WHERE id=?", (main, dup))
    conn.execute("INSERT INTO complaints(vacancy_id, reason) VALUES (?, 'x')", (main,))
    recent = save_vacancy(conn, _vac(30, chan="c", ext="3", profession="повар"))  # истёк, но ещё в хранении
    conn.commit()
    assert lifecycle.purge(conn, 21, 30, NOW) == 2
    ids = {r[0] for r in conn.execute("SELECT id FROM vacancies")}
    assert ids == {recent}
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0


def test_migration_adds_column_to_old_database(tmp_path):
    import sqlite3

    from rabota.db import SCHEMA

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA.replace("last_seen_at TEXT, ", ""))  # база до появления срока жизни
    assert "last_seen_at" not in {r[1] for r in old.execute("PRAGMA table_info(vacancies)")}
    old.execute("INSERT INTO vacancies(profession, published_at) VALUES ('бариста', '2026-10-01T00:00:00+00:00')")
    old.commit()
    old.close()
    conn = connect(path)
    row = conn.execute("SELECT last_seen_at FROM vacancies").fetchone()
    assert row["last_seen_at"] == "2026-10-01T00:00:00+00:00"
