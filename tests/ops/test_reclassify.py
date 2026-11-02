"""reclassify: пересчёт должности, отрасли и формата у сохранённых вакансий, остальное не трогаем."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rabota.db import connect, save_raw, save_vacancy
from rabota.models import RawPost, Vacancy
from rabota.ops.reclassify import reclassify

FIXTURE = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "hh" / "vacancy_prohodchik_vorkuta.json").read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    c = connect(":memory:")
    save_raw(c, RawPost(**FIXTURE))
    return c


def broken(conn, **over) -> int:
    """Вакансия в том виде, в каком её испортил старый разбор."""
    vac = Vacancy(
        profession="сотрудник", category="медицина", work_format="удалённо", employment="постоянно", city="Донецк",
        pay_min=230000, pay_max=300000, pay_period="месяц", title_raw=FIXTURE["extra"]["name"],
        source_type="hh", source="hh.ru", source_url=FIXTURE["url"], external_id=FIXTURE["external_id"], status="active",
    )
    for k, v in over.items():
        setattr(vac, k, v)
    return save_vacancy(conn, vac)


def row(conn, vid):
    return dict(conn.execute("SELECT * FROM vacancies WHERE id = ?", (vid,)).fetchone())


def test_fixes_title_category_format_and_keeps_the_rest(conn):
    vid = broken(conn)
    before = row(conn, vid)
    res = reclassify(conn)
    after = row(conn, vid)
    assert res["changed"] == 1
    assert (after["profession"], after["category"], after["work_format"]) == ("проходчик", "производство", "вахта")
    keep = ("id", "city", "pay_min", "pay_max", "pay_period", "status", "moderation_status", "employment", "title_raw", "summary", "last_seen_at")
    assert {k: after[k] for k in keep} == {k: before[k] for k in keep}


def test_dry_run_changes_nothing(conn):
    vid = broken(conn)
    before = row(conn, vid)
    res = reclassify(conn, dry_run=True)
    assert res["changed"] == 1 and res["items"][0]["diff"]["profession"] == ("сотрудник", "проходчик")
    assert row(conn, vid) == before


def test_idempotent(conn):
    broken(conn)
    reclassify(conn)
    assert reclassify(conn)["changed"] == 0


def test_owner_edited_and_non_parsed_are_not_touched(conn):
    owned = broken(conn)
    conn.execute("UPDATE vacancies SET owner_edited_at = '2026-11-01T10:00:00+00:00' WHERE id = ?", (owned,))
    manual = broken(conn, external_id="x")
    conn.execute("UPDATE vacancies SET origin = 'employer' WHERE id = ?", (manual,))
    conn.commit()
    res = reclassify(conn)
    assert res["checked"] == 0 and res["changed"] == 0
    assert row(conn, owned)["profession"] == "сотрудник"


def test_source_filter(conn):
    broken(conn)
    assert reclassify(conn, source="telegram")["checked"] == 0
    assert reclassify(conn, source="hh")["changed"] == 1
