"""Стоп-лист в конвейере: разбор сырых постов и приём в реальном времени не публикуют вакансии из списка."""
from rabota.admin import stoplist
from rabota.models import RawPost
from rabota.pipeline.run import process_db
from rabota.realtime.ingest import ingest_post
from tests.admin.helpers import ADMIN

GOOD = "Требуется бариста в кофейню, Донецк, Ворошиловский район. График 2/2, оплата 4000 за смену. Звоните +79490000001"
BAD = "Требуется бариста в кофейню, Донецк, Ворошиловский район. График 2/2, оплата 4000 за смену. Звоните +79490000002"


def raw(i, text):
    return RawPost("telegram", "chan", str(i), f"https://t.me/chan/{i}", text, "2026-10-29T10:00:00+00:00")


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]


def test_process_db_skips_stoplisted_phone(conn):
    from rabota.db import save_raw

    stoplist.add(conn, "phone", "+79490000002", actor=ADMIN)
    save_raw(conn, raw(1, GOOD))
    save_raw(conn, raw(2, BAD))
    stats = process_db(conn)
    assert count(conn) == 1 and stats["stoplisted"] == 1
    assert conn.execute("SELECT contacts FROM vacancies").fetchone()[0].count("0001") == 1
    assert stoplist.listing(conn)[0]["hits"] == 1


def test_process_db_keyword(conn):
    from rabota.db import save_raw

    stoplist.add(conn, "keyword", "кофейню", actor=ADMIN)
    save_raw(conn, raw(1, GOOD))
    process_db(conn)
    assert count(conn) == 0


def test_process_db_without_stoplist_unchanged(conn):
    from rabota.db import save_raw

    save_raw(conn, raw(1, GOOD))
    stats = process_db(conn)
    assert count(conn) == 1 and stats["stoplisted"] == 0


def test_ingest_post_skips_stoplisted(conn):
    stoplist.add(conn, "phone", "+79490000002", actor=ADMIN)
    res = ingest_post(conn, raw(1, BAD), provider=object())
    assert res["new"] and res["vacancies"] == 0 and res["stoplisted"] == 1
    assert count(conn) == 0
    ok = ingest_post(conn, raw(2, GOOD), provider=object())
    assert ok["vacancies"] == 1 and count(conn) == 1


def test_ingest_post_is_idempotent_for_stoplisted(conn):
    stoplist.add(conn, "phone", "+79490000002", actor=ADMIN)
    ingest_post(conn, raw(1, BAD), provider=object())
    again = ingest_post(conn, raw(1, BAD), provider=object())
    assert again["new"] is False and count(conn) == 0


def test_stoplist_added_later_blocks_next_post_only(conn):
    ingest_post(conn, raw(1, BAD), provider=object())
    assert count(conn) == 1
    stoplist.add(conn, "phone", "+79490000002", actor=ADMIN)
    ingest_post(conn, raw(3, BAD.replace("бариста", "повар")), provider=object())
    assert count(conn) == 1
