from datetime import timedelta

import pytest

from rabota.web import queries as q
from tests.web.conftest import NOW


def prof(conn, f=None, page=1):
    return [v["profession"] for v in q.search(conn, f or q.Filters(), page).items]


def test_only_listable_vacancies_are_shown(conn, add):
    ok = add(profession="бариста")
    add(profession="военная служба", kind="military")
    add(profession="репетитор", kind="service")
    add(profession="реклама", kind="ad")
    add(profession="закрытая", status="closed")
    add(profession="скрытая", status="hidden")
    add(profession="мнимая", suspicious=True)
    add(profession="дубль", duplicate_of=ok)
    assert prof(conn) == ["бариста"]
    assert q.stats(conn, NOW)["total"] == 1
    assert [v["profession"] for v in q.latest(conn)] == ["бариста"]


def test_sorted_fresh_first(conn, add):
    add(profession="старая", ago=500)
    add(profession="свежая", ago=5)
    add(profession="средняя", ago=60)
    assert prof(conn) == ["свежая", "средняя", "старая"]


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("кассир", True), ("КАССИР", True), ("Кассира", True), ("кассиров", True), ("кассиры", True),
        ("продавец", True), ("продавца", True), ("Продавцы", True),
        ("водитель", False),
    ],
)
def test_search_ignores_case_and_endings(conn, add, query, found):
    add(profession="кассир")
    add(profession="продавец")
    names = prof(conn, q.Filters(q=query))
    assert bool(names) is found


def test_search_looks_into_summary_title_employer_city(conn, add):
    add(profession="повар", summary="Готовим борщи и пельмени", title_raw="Требуется ПОВАР", employer="Кафе «Лето»")
    add(profession="слесарь", city="Горловка")
    assert prof(conn, q.Filters(q="пельмени")) == ["повар"]
    assert prof(conn, q.Filters(q="лето")) == ["повар"]
    assert prof(conn, q.Filters(q="Горловке")) == ["слесарь"]  # падеж города тоже не мешает
    assert prof(conn, q.Filters(q="горловка")) == ["слесарь"]


def test_search_every_word_must_match_and_profession_ranks_first(conn, add):
    add(profession="грузчик", summary="помогать бариста", ago=1)
    add(profession="бариста", ago=100)
    assert prof(conn, q.Filters(q="бариста")) == ["бариста", "грузчик"]
    assert prof(conn, q.Filters(q="бариста донецк")) == ["бариста", "грузчик"]
    assert prof(conn, q.Filters(q="бариста макеевка")) == []


def test_search_escapes_like_wildcards(conn, add):
    add(profession="кассир")
    assert prof(conn, q.Filters(q="%")) == []
    assert prof(conn, q.Filters(q="к_ссир")) == []
    assert prof(conn, q.Filters(q="'; DROP TABLE vacancies; --")) == []
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_yo_is_the_same_as_ye(conn, add):
    add(profession="учёный секретарь")
    assert prof(conn, q.Filters(q="ученый")) == ["учёный секретарь"]


def test_filters_combine(conn, add):
    add(profession="а", city="Донецк", district="Ворошиловский", category="общепит", work_format="точка",
        pay_period="смена", pay_daily=True, experience="не нужен", employment="подработка", age_min=18)
    add(profession="б", city="Макеевка", category="склад", work_format="склад", pay_period="месяц", age_min=21)
    add(profession="в", city="Донецк", district="Калининский", category="офис", work_format="удалённо", pay_period="месяц")
    assert prof(conn, q.Filters(city="Донецк")) == ["а", "в"] or sorted(prof(conn, q.Filters(city="Донецк"))) == ["а", "в"]
    assert prof(conn, q.Filters(district="ворошиловский")) == ["а"]
    assert prof(conn, q.Filters(category="склад")) == ["б"]
    assert prof(conn, q.Filters(work_format="удалённо")) == ["в"]
    assert prof(conn, q.Filters(pay_period="смена")) == ["а"]
    assert prof(conn, q.Filters(daily=True)) == ["а"]
    assert prof(conn, q.Filters(no_experience=True)) == ["а"]
    assert prof(conn, q.Filters(side_job=True)) == ["а"]
    assert sorted(prof(conn, q.Filters(adult=True))) == ["а", "в"]  # возраст до 18 или не указан
    assert prof(conn, q.Filters(city="Донецк", category="офис", pay_period="месяц")) == ["в"]
    assert prof(conn, q.Filters(city="Макеевка", daily=True)) == []


def test_no_experience_variants(conn, add):
    add(profession="а", experience="не нужен")
    add(profession="б", experience="Без опыта")
    add(profession="в", experience="от 1 года")
    add(profession="г")
    assert sorted(prof(conn, q.Filters(no_experience=True))) == ["а", "б"]


def test_side_job_includes_today(conn, add):
    add(profession="а", employment="подработка")
    add(profession="б", employment="на сегодня")
    add(profession="в", employment="постоянно")
    assert sorted(prof(conn, q.Filters(side_job=True))) == ["а", "б"]


def test_pagination(conn, add):
    for i in range(45):
        add(profession=f"проф{i:02d}", ago=i)
    p1 = q.search(conn, q.Filters(), 1, per_page=20)
    p3 = q.search(conn, q.Filters(), 3, per_page=20)
    assert (p1.total, p1.pages, len(p1.items), p1.has_next, p1.has_prev) == (45, 3, 20, True, False)
    assert len(p3.items) == 5 and not p3.has_next and p3.has_prev
    assert q.search(conn, q.Filters(), 99, per_page=20).page == 3  # слишком большая страница прижимается
    assert q.search(conn, q.Filters(), 0, per_page=20).page == 1
    ids = [v["id"] for pg in (1, 2, 3) for v in q.search(conn, q.Filters(), pg, per_page=20).items]
    assert len(set(ids)) == 45


def test_parse_page_is_safe():
    assert q.parse_page("abc") == 1
    assert q.parse_page("-5") == 1
    assert q.parse_page("7") == 7
    assert q.parse_page(None) == 1
    assert q.parse_page("99999999999") == 10_000


def test_filters_from_params_cleans_input():
    f = q.Filters.from_params({"q": "  бариста   ", "city": "Донецк" * 50, "daily": "on", "adult": "0"})
    assert f.q == "бариста" and len(f.city) == 60 and f.daily and not f.adult
    assert f.active_count == 2  # город и ежедневная выплата


def test_query_string_roundtrip():
    f = q.Filters(q="бариста", city="Донецк", daily=True)
    s = f.query_string()
    assert "daily=1" in s and "no_experience" not in s
    assert "city=" not in f.query_string(city="")


def test_similar_prefers_same_profession_then_category(conn, add):
    main = add(profession="бариста", city="Донецк")
    add(profession="бариста", city="Донецк", ago=10)
    add(profession="повар", category="общепит", ago=5)
    add(profession="водитель", category="транспорт")
    add(profession="бариста", status="closed")
    v = q.get_vacancy(conn, main)
    names = [s["profession"] for s in q.similar(conn, v)]
    assert names == ["бариста", "повар"]
    assert main not in [s["id"] for s in q.similar(conn, v)]


def test_facets_and_stats(conn, add):
    add(profession="а", category="офис", city="Донецк", district="Ворошиловский", pay_daily=True, experience="не нужен", ago=10)
    add(profession="а", category="офис", city="Макеевка", ago=60 * 30)
    add(profession="военные", category="другое", kind="military")
    f = q.facets(conn)
    assert f["categories"] == [("офис", 2)]
    assert dict(f["cities"]) == {"Донецк": 1, "Макеевка": 1}
    assert f["professions"] == [("а", 2)]
    s = q.stats(conn, NOW)
    assert s == {"total": 2, "fresh": 1, "no_experience": 1, "daily": 1, "side_job": 0}


def test_stats_fresh_uses_24_hours(conn, add):
    add(ago=60 * 23)
    add(ago=60 * 25)
    assert q.stats(conn, NOW)["fresh"] == 1
    assert (NOW - timedelta(hours=24)).isoformat() > "2026-10-28"


def test_add_site_vacancy_premoderation(conn):
    from tests.web.conftest import make_vacancy

    vid = q.add_site_vacancy(conn, make_vacancy(profession="курьер"), premoderate=True)
    row = q.get_vacancy(conn, vid)
    assert row["source_type"] == "site" and row["status"] == "hidden" and row["kind"] == "vacancy"
    assert prof(conn) == []
    vid2 = q.add_site_vacancy(conn, make_vacancy(profession="курьер"), premoderate=False)
    assert q.get_vacancy(conn, vid2)["status"] == "active"
    assert prof(conn) == ["курьер"]
