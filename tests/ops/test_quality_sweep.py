"""quality-sweep: скрыть активные вакансии с мусорным названием, идемпотентно, не трогая нормальные и одобренные вручную."""
from __future__ import annotations

import pytest

from rabota import __main__ as cli
from rabota.admin import audit, moderation
from rabota.db import connect, save_vacancy
from rabota.models import Vacancy
from rabota.ops.quality_sweep import find_junk, sweep
from rabota.pipeline.quality import NOTE_PREFIX
from rabota.web import queries


@pytest.fixture
def conn():
    return connect(":memory:")


def legacy(conn, profession: str, ext: str) -> int:
    """Вакансия, попавшая в базу до фильтра: активна, одобрена, не помечена."""
    vid = save_vacancy(
        conn, Vacancy(profession=profession, source_type="telegram", source="Vakansiii_dnr", external_id=ext, contacts={"phones": ["+79495055755"]})
    )
    conn.execute("UPDATE vacancies SET suspicious=0, status='active', moderation_status='approved', moderation_note=NULL WHERE id=?", (vid,))
    conn.commit()
    return vid


def row(conn, vid):
    return dict(conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone())


def test_sweep_hides_junk_with_reason_and_keeps_good(conn):
    junk, ok = legacy(conn, "„оо", "1"), legacy(conn, "грузчик", "2")
    res = sweep(conn)
    assert res["checked"] == 2 and res["junk"] == 1 and res["hidden"] == 1
    bad = row(conn, junk)
    assert bad["suspicious"] == 1 and bad["status"] == "hidden" and bad["moderation_status"] == "pending"
    assert bad["moderation_note"].startswith(NOTE_PREFIX) and "„оо" in bad["moderation_note"]
    good = row(conn, ok)
    assert good["status"] == "active" and good["suspicious"] == 0 and good["moderation_status"] == "approved"


def test_sweep_is_idempotent(conn):
    legacy(conn, "...", "1")
    legacy(conn, "аааа", "2")
    assert sweep(conn)["hidden"] == 2
    again = sweep(conn)
    assert again["junk"] == 0 and again["hidden"] == 0 and again["checked"] == 0
    assert audit.count(conn, "vacancy.quality_hide") == 2  # повторный запуск журнал не множит


def test_dry_run_changes_nothing(conn):
    junk = legacy(conn, "---", "1")
    before = row(conn, junk)
    res = sweep(conn, dry_run=True)
    assert res["junk"] == 1 and res["hidden"] == 0 and res["dry_run"] and res["items"][0]["id"] == junk
    assert row(conn, junk) == before
    assert audit.count(conn, "vacancy.quality_hide") == 0


def test_junk_leaves_search_and_sitemap_but_shows_in_moderation(conn):
    junk, ok = legacy(conn, "„оо", "1"), legacy(conn, "грузчик", "2")
    sweep(conn)
    conn.create_function("rfold", 1, lambda v: (v or "").lower().replace("ё", "е"), deterministic=True)
    visible = [r["id"] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")]
    assert visible == [ok]
    assert [i for i, _ in queries.sitemap_entries(conn, 1)["vacancies"]] == [ok]
    queue = moderation.queue(conn, "pending")
    assert [v["id"] for v in queue.items] == [junk] and queue.items[0]["moderation_note"].startswith(NOTE_PREFIX)


def test_vacancy_approved_by_admin_is_not_hidden_again(conn):
    vid = legacy(conn, "„оо", "1")
    sweep(conn)
    assert moderation.approve(conn, vid, {"id": None, "email": "admin@example.com"})
    approved = row(conn, vid)
    assert approved["suspicious"] == 0 and approved["moderation_status"] == "approved" and approved["status"] == "active"
    assert sweep(conn)["junk"] == 0  # решение администратора главнее правила
    assert row(conn, vid)["status"] == "active"


def test_duplicates_and_other_kinds_are_ignored(conn):
    dup = legacy(conn, "„оо", "1")
    conn.execute("UPDATE vacancies SET duplicate_of = 999 WHERE id=?", (dup,))
    ad = legacy(conn, "..", "2")
    conn.execute("UPDATE vacancies SET kind='ad' WHERE id=?", (ad,))
    conn.commit()
    assert find_junk(conn) == []


def test_cli_quality_sweep(conn, monkeypatch, tmp_path, capsys):
    path = tmp_path / "t.db"
    c = connect(path)
    legacy(c, "„оо", "1")
    c.close()
    monkeypatch.setattr(cli.config, "DB_PATH", path)
    assert cli.main(["quality-sweep", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "мусорных 1, скрыто 0" in out and "пробный запуск" in out
    assert cli.main(["quality-sweep"]) == 0
    assert "скрыто 1" in capsys.readouterr().out
    assert cli.main(["quality-sweep"]) == 0
    assert "мусорных 0" in capsys.readouterr().out
