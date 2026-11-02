"""Архив просроченных вакансий: перенос, очистка текста, срок хранения, оживление без дубля, выдача и страница компании."""
from __future__ import annotations

from datetime import timedelta

import pytest

from rabota.admin import settings
from rabota.db import connect, save_raw
from rabota.employers_auto import archive, cards
from rabota.models import RawPost
from rabota.realtime.ingest import ingest_post
from rabota.web import queries as q
from tests.employers_auto.kit import NOW, auto_company, vacancy

TEXT = "Требуется бариста в кофейню, Донецк, Ворошиловский район. График 2/2, оплата 4000 за смену. Звоните +79490000001"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "arch.db")
    yield c
    c.close()


def status_of(conn, vid):
    return conn.execute("SELECT status, archived_at, last_seen_at FROM vacancies WHERE id=?", (vid,)).fetchone()


def old_vacancy(conn, days=100, **kw):
    seen = (NOW - timedelta(days=days)).replace(microsecond=0).isoformat()
    vid = vacancy(conn, kw.pop("employer", "Кофейня Зерно"), published_at=seen, last_seen_at=seen, **kw)
    conn.execute("UPDATE vacancies SET status='expired' WHERE id=?", (vid,))
    conn.commit()
    return vid


def test_default_settings():
    assert settings.effective(None, "archive_keep_days") == 365
    assert settings.effective(None, "archive_raw_days") == 30


def test_archive_keeps_row_and_hides_from_site(conn):
    vid = old_vacancy(conn)
    assert archive.archive_expired(conn, 21, 30, NOW) == 1
    row = status_of(conn, vid)
    assert row["status"] == "archived" and row["archived_at"]
    assert conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {q.VISIBLE_SQL}").fetchone()[0] == 0
    assert archive.archive_expired(conn, 21, 30, NOW) == 0, "повторный запуск ничего не меняет"


def test_recently_expired_stays_expired(conn):
    vid = old_vacancy(conn, days=30)
    assert archive.archive_expired(conn, 21, 30, NOW) == 0
    assert status_of(conn, vid)["status"] == "expired"


def test_closed_by_employer_also_goes_to_archive(conn):
    vid = old_vacancy(conn)
    conn.execute("UPDATE vacancies SET status='closed' WHERE id=?", (vid,))
    conn.commit()
    assert archive.archive_expired(conn, 21, 30, NOW) == 1


def test_raw_text_cleared_after_n_days_but_card_fields_stay(conn):
    vid = old_vacancy(conn, external_id="r1")
    save_raw(conn, RawPost("telegram", "@demo", "r1", "https://t.me/demo/r1", TEXT, None, ["https://img/1.jpg"], {"a": 1}))
    conn.commit()
    archive.archive_expired(conn, 21, 30, NOW)
    assert archive.clear_raw(conn, 30, NOW) == 0, "архивной записи меньше 30 дней"
    assert archive.clear_raw(conn, 30, NOW + timedelta(days=31)) == 1
    raw = conn.execute("SELECT text, image_urls, extra FROM raw_posts WHERE external_id='r1'").fetchone()
    assert (raw["text"], raw["image_urls"], raw["extra"]) == ("", "[]", "{}")
    v = conn.execute("SELECT profession, city, contacts FROM vacancies WHERE id=?", (vid,)).fetchone()
    assert v["profession"] == "бариста" and v["city"] == "Донецк" and "+7949" in v["contacts"]
    assert archive.clear_raw(conn, 30, NOW + timedelta(days=31)) == 0


def test_purge_after_archive_period_removes_everything(conn):
    vid = old_vacancy(conn, external_id="p1")
    dup = old_vacancy(conn, external_id="p2", source="@other")
    conn.execute("UPDATE vacancies SET duplicate_of=? WHERE id=?", (vid, dup))
    save_raw(conn, RawPost("telegram", "@demo", "p1", "u", TEXT))
    conn.commit()
    archive.archive_expired(conn, 21, 30, NOW)
    assert archive.purge_archive(conn, 365, NOW + timedelta(days=364)) == 0
    assert archive.purge_archive(conn, 365, NOW + timedelta(days=366)) == 2
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_posts WHERE external_id='p1'").fetchone()[0] == 0


def test_keep_days_setting_is_used(conn):
    old_vacancy(conn)
    archive.archive_expired(conn, 21, 30, NOW)
    settings.save(conn, {"id": 1, "email": "a@example.test", "role": "admin"}, {"archive_keep_days": "30"}, now=NOW)
    assert archive.keep_days(conn) == 30
    assert archive.purge_archive(conn, None, NOW + timedelta(days=31)) == 1


def test_run_all_report(conn):
    old_vacancy(conn)
    res = archive.run_all(conn, NOW)
    assert res == {"в архив": 1, "очищено постов": 0, "удалено": 0}


def test_manual_revive(conn):
    vid = old_vacancy(conn)
    archive.archive_expired(conn, 21, 30, NOW)
    assert archive.revive(conn, vid, NOW) is True
    row = status_of(conn, vid)
    assert row["status"] == "active" and row["archived_at"] is None
    assert archive.revive(conn, vid, NOW) is False


def test_recollected_post_revives_without_duplicate(conn):
    """Тот же пост собрали снова с новой датой: вакансия оживает, второй строки нет."""
    res = ingest_post(conn, RawPost("telegram", "chan_a", "1", "https://t.me/chan_a/1", TEXT, "2020-01-01T10:00:00+00:00"))
    assert res["vacancies"] == 1
    vid = conn.execute("SELECT id FROM vacancies").fetchone()[0]
    archive.archive_expired(conn, 21, 30)
    assert status_of(conn, vid)["status"] == "archived"
    # дата в прошлом: оживления нет
    ingest_post(conn, RawPost("telegram", "chan_a", "1", "https://t.me/chan_a/1", TEXT, "2020-01-01T10:00:00+00:00"))
    assert status_of(conn, vid)["status"] == "archived"
    from datetime import UTC, datetime
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    again = ingest_post(conn, RawPost("telegram", "chan_a", "1", "https://t.me/chan_a/1", TEXT, now_iso))
    assert again["new"] is False and again.get("revived") == 1
    assert status_of(conn, vid)["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_repost_in_other_channel_revives_archived_main(conn):
    ingest_post(conn, RawPost("telegram", "chan_a", "1", "https://t.me/chan_a/1", TEXT, "2020-01-01T10:00:00+00:00"))
    main = conn.execute("SELECT id FROM vacancies").fetchone()[0]
    archive.archive_expired(conn, 21, 30)
    from datetime import UTC, datetime
    ingest_post(conn, RawPost("telegram", "chan_b", "9", "https://t.me/chan_b/9", TEXT, datetime.now(UTC).replace(microsecond=0).isoformat()))
    row = status_of(conn, main)
    assert row["status"] == "active" and row["archived_at"] is None
    assert conn.execute("SELECT duplicate_of FROM vacancies WHERE external_id='9'").fetchone()[0] == main


def test_owner_edit_blocks_collector_overwrite(conn):
    vid = vacancy(conn, "Кофейня Зерно", external_id="o1")
    conn.execute("UPDATE vacancies SET owner_edited_at=?, profession='кондитер' WHERE id=?", (NOW.isoformat(), vid))
    conn.commit()
    from datetime import UTC, datetime
    ingest_post(conn, RawPost("telegram", "@demo", "other", "https://t.me/demo/other", TEXT, datetime.now(UTC).replace(microsecond=0).isoformat()))
    assert conn.execute("SELECT profession FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == "кондитер"


def test_company_archive_lists_only_archived_of_company(conn):
    cid = auto_company(conn, "Кофейня Зерно", count=2)
    other = auto_company(conn, "Пекарня Хлеб", count=1, source="@x")
    ids = [r[0] for r in conn.execute("SELECT id FROM vacancies WHERE company_id=? ORDER BY id", (cid,))]
    conn.execute("UPDATE vacancies SET status='archived', archived_at=? WHERE id=?", (NOW.isoformat(), ids[0]))
    conn.execute("UPDATE vacancies SET status='archived' WHERE company_id=?", (other,))
    conn.commit()
    items, total = archive.company_archive(conn, cid)
    assert total == 1 and items[0]["id"] == ids[0]
    assert cards.counts(conn, cid) == {"visible": 1, "archived": 1, "total": 2}
    assert archive.company_archive(conn, cid, page=99)[1] == 1, "страница за пределами не ломает"


def test_archive_skips_hidden_or_suspicious_in_company_list(conn):
    cid = auto_company(conn, "Кофейня Зерно", count=2)
    ids = [r[0] for r in conn.execute("SELECT id FROM vacancies WHERE company_id=?", (cid,))]
    conn.execute("UPDATE vacancies SET status='archived', suspicious=1 WHERE id=?", (ids[0],))
    conn.execute("UPDATE vacancies SET status='archived', moderation_status='rejected' WHERE id=?", (ids[1],))
    conn.commit()
    assert archive.company_archive(conn, cid)[1] == 0


def test_archived_vacancy_not_in_sitemap(tmp_path):
    c = q.open_conn(tmp_path / "sm.db")
    try:
        vid = old_vacancy(c)
        fresh = vacancy(c, "Кофейня Зерно", external_id="fresh")
        assert vid != fresh
        archive.archive_expired(c, 21, 30, NOW)
        ids = {i for i, _ in q.sitemap_entries(c)["vacancies"]}
        assert vid not in ids and fresh in ids
    finally:
        c.close()


def test_admin_history_shows_archive_step(conn):
    from rabota.admin import vacancies as admin_vacancies

    vid = old_vacancy(conn)
    archive.archive_expired(conn, 21, 30, NOW)
    titles = [e["title"] for e in admin_vacancies.history(conn, admin_vacancies.get(conn, vid), NOW)]
    assert "Срок жизни истёк" in titles and "Перенесена в архив" in titles
