"""Защитный фильтр от мусора: правила названия должности и сохранение мусорной вакансии на проверку."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rabota.db import connect, save_vacancy
from rabota.models import RawPost, Vacancy
from rabota.pipeline.quality import NOTE_PREFIX, is_quality_note, profession_problem, vacancy_problem
from rabota.pipeline.run import analyze

LABELED = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "pipeline" / "labeled.json").read_text(encoding="utf-8"))

JUNK = ["„оо", "„зонт", "студию}", "новую студию |", "...", "---", "аааа", "123", "", "   ", "и", "по и на", "ab", "оо", "!!!???", "«»", "ххххх", "qwrtp", "бмфчтсь"]
GOOD = [
    "грузчик", "SMM-менеджер", "1С-программист", "ИТ специалист", "продавец-консультант", "повар", "няня", "швея",
    "Автомойщик", "бухгалтер", "водитель категории B", "администратор-кассир", "флорист-декоратор", "су-шеф",
]


@pytest.mark.parametrize("title", JUNK)
def test_real_junk_is_rejected(title):
    assert profession_problem(title), title


@pytest.mark.parametrize("title", GOOD)
def test_normal_professions_pass(title):
    assert profession_problem(title) is None, title


def test_service_symbols_share_over_thirty_percent_is_junk():
    assert "знаки" in (profession_problem("„оо") or "")
    assert profession_problem("«грузчик»") is None  # кавычки по краям не делают слово мусором


def test_every_labeled_profession_and_parsed_vacancy_passes():
    for ex in LABELED:
        for v in ex.get("vacancies") or []:
            assert profession_problem(v["profession"]) is None, v["profession"]
        for vac in analyze(RawPost("telegram", "s", ex["id"], "u", ex["text"])).vacancies:
            assert vacancy_problem(vac) is None, (ex["id"], vac.profession)


def test_vacancy_problem_ignores_non_vacancy_kinds():
    assert vacancy_problem(Vacancy(profession="..", kind="ad")) is None
    assert vacancy_problem({"profession": "..", "kind": "vacancy"})


def vac(profession="грузчик", **kw) -> Vacancy:
    return Vacancy(profession=profession, source_type="telegram", source="s", external_id=profession, contacts={"phones": ["+79491112233"]}, **kw)


def test_save_vacancy_sends_junk_to_moderation_and_keeps_good_live():
    conn = connect(":memory:")
    bad = save_vacancy(conn, vac("„оо"), check_quality=True)
    good = save_vacancy(conn, vac("грузчик"), check_quality=True)
    row = dict(conn.execute("SELECT * FROM vacancies WHERE id=?", (bad,)).fetchone())
    assert row["suspicious"] == 1 and row["status"] == "hidden" and row["moderation_status"] == "pending"
    assert is_quality_note(row["moderation_note"]) and row["moderation_note"].startswith(NOTE_PREFIX)
    ok = dict(conn.execute("SELECT * FROM vacancies WHERE id=?", (good,)).fetchone())
    assert ok["suspicious"] == 0 and ok["status"] == "active" and ok["moderation_status"] == "approved"


def test_junk_vacancy_is_not_in_search_or_sitemap_queries():
    from rabota.web import queries

    conn = connect(":memory:")
    save_vacancy(conn, vac("„оо"), check_quality=True)
    save_vacancy(conn, vac("грузчик"), check_quality=True)
    conn.create_function("rfold", 1, lambda v: (v or "").lower().replace("ё", "е"), deterministic=True)
    visible = [r["profession"] for r in conn.execute(f"SELECT profession FROM vacancies WHERE {queries.VISIBLE_SQL}")]
    assert visible == ["грузчик"]
    assert [i for i, _ in queries.sitemap_entries(conn, 1)["vacancies"]] == [2]


def test_default_save_does_not_filter_so_plain_callers_are_unchanged():
    conn = connect(":memory:")
    vid = save_vacancy(conn, vac("а"))
    assert conn.execute("SELECT suspicious, status FROM vacancies WHERE id=?", (vid,)).fetchone()[:] == (0, "active")


def test_pipeline_paths_filter_junk(monkeypatch):
    """Разбор постов (process_db) сохраняет мусорное название на проверку, а не в выдачу."""
    from rabota.pipeline import run

    conn = connect(":memory:")
    raw = RawPost("telegram", "c", "1", "https://t.me/c/1", "Требуется повар в кафе, Донецк, оплата 3500 за смену, звоните +79491112233")
    monkeypatch.setattr(run, "analyze", lambda r: run.Analysis("vacancy", "", [Vacancy(profession="„оо", source_type="telegram", source="c", external_id="1", contacts={"phones": ["+79491112233"]})]))
    from rabota.db import save_raw

    save_raw(conn, raw)
    assert run.process_db(conn)["vacancies"] == 1
    row = conn.execute("SELECT suspicious, status, moderation_status FROM vacancies").fetchone()
    assert tuple(row) == (1, "hidden", "pending")
