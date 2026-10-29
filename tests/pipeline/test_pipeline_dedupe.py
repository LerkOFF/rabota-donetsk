"""Склейка дублей между источниками: в памяти и в базе."""
import pytest

from rabota.db import connect, save_raw, save_vacancy
from rabota.models import RawPost, Vacancy
from rabota.pipeline.dedupe import (
    SIMILARITY_THRESHOLD,
    also_published_in,
    assign_duplicates,
    canonical_phone,
    dedupe,
    make_dedupe_key,
    merge_into,
    shingles,
    similarity,
    text_normalize,
)
from rabota.pipeline.run import process, process_db

BASE = (
    "Требуется бухгалтер в торговую компанию. Донецк. Обязанности: ведение ИП (УСН, НДС), работа с первичной "
    "документацией, контроль дебиторки и кредиторки, отчетность. Требования: профильное образование, опыт от 1 года, "
    "знание 1С. График 5/2 с 9:00 до 17:00. Офис в центре города."
)


def post(text, source_type, source, ext, published_at, phone="+7 949 000-00-01"):
    full = f"{text}\n{('Тел. ' + phone) if phone else ''}"
    return RawPost(source_type, source, ext, f"https://example.com/{source}/{ext}", full, published_at=published_at)


def one(raw):
    (v,) = process(raw)
    return v


def marked(vacancies):
    return [v for v in vacancies if v.duplicate_of is not None]


def test_canonical_phone_spellings():
    for s in ("+7 (949) 000-00-01", "8 949 000 00 01", "79490000001", "8-949-000-00-01", "949 000 00 01"):
        assert canonical_phone(s) == "+79490000001"
    assert canonical_phone("12345") is None


def test_text_normalize_ignores_decoration_and_phones():
    a = text_normalize("🔥 СРОЧНО!!! Требуется повар #работа\n+7 949 000-00-01 https://t.me/x")
    b = text_normalize("Срочно требуется повар")
    assert a == b


def test_make_dedupe_key_phone_spellings_equal():
    k1 = make_dedupe_key("повар", ["+79490000001"])
    k2 = make_dedupe_key("повар", ["+79490000001", "+79490000002"])
    assert k1 == k2
    assert k1 != make_dedupe_key("официант", ["+79490000001"])


def test_similarity_bounds():
    a = shingles(text_normalize(BASE))
    assert similarity(a, a) == 1.0
    assert similarity(a, shingles(text_normalize("Продаём квартиры недорого в центре города"))) == 0.0
    assert similarity(frozenset(), a) == 0.0


def test_telegram_max_vk_with_edits_become_one():
    tg = one(post(BASE, "telegram", "chan", "10", "2026-10-28T10:00:00+00:00"))
    mx = one(post(BASE.replace("Требуется", "Срочно требуется") + " Резюме на почту.", "max", "web", "20",
                  "2026-10-28T12:00:00+00:00", phone="8 949 000 00 01"))
    vk = one(post("🔥 " + BASE.upper(), "vk", "club1", "30", "2026-10-29T09:00:00+00:00", phone="79490000001"))
    vs = assign_duplicates([vk, mx, tg], ids=[3, 2, 1])
    assert tg.duplicate_of is None
    assert mx.duplicate_of == 1 and vk.duplicate_of == 1
    assert len(marked(vs)) == 2


def test_dup_found_by_text_without_phone():
    a = one(post(BASE, "telegram", "chan", "1", "2026-10-28T10:00:00+00:00", phone=""))
    b = one(post(BASE + " Звоните!", "vk", "club", "2", "2026-10-28T11:00:00+00:00", phone=""))
    assign_duplicates([a, b])
    assert b.duplicate_of == 1 and a.duplicate_of is None


def test_same_phone_different_professions_not_merged():
    a = one(post("Требуется повар в кафе. Донецк. Оплата 4000 за смену.", "telegram", "c", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post("Требуется официант в кафе. Донецк. Оплата 3500 за смену.", "telegram", "c", "2", "2026-10-28T10:05:00+00:00"))
    assign_duplicates([a, b])
    assert a.duplicate_of is None and b.duplicate_of is None


def test_same_profession_different_text_and_no_phone_not_merged():
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00", phone=""))
    other = (
        "Нужен бухгалтер на склад оптовой базы в Макеевке, зарплата 60000, расчёт по зарплате и материалам, "
        "склад, накладные, инвентаризация, учёт движения товара, опыт обязателен, график шесть дней"
    )
    b = one(post(other, "vk", "club", "2", "2026-10-28T11:00:00+00:00", phone=""))
    assign_duplicates([a, b])
    assert b.duplicate_of is None


def test_same_phone_and_profession_but_different_city_not_merged():
    a = one(post("Требуется продавец в магазин. Донецк. Оплата 4000 за смену.", "telegram", "c", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post("Требуется продавец в магазин. Макеевка. Оплата 4000 за смену.", "telegram", "d", "2", "2026-10-28T10:05:00+00:00"))
    assign_duplicates([a, b])
    assert b.duplicate_of is None


def test_same_phone_but_different_pay_and_text_not_merged():
    a = one(post("Требуется повар в заготовочный цех. Оплата 5000 за смену. Донецк, Макеевка, ул. Дальневосточная.",
                 "vk", "club1", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post("Обучение работе повара, оплачиваемая работа с первого дня. Оплата 2000-4000 выход. Донецк.",
                 "telegram", "chan", "2", "2026-10-28T11:00:00+00:00"))
    assign_duplicates([a, b])
    assert b.duplicate_of is None


def test_threshold_controls_text_match():
    edited = BASE.replace("Обязанности: ведение ИП (УСН, НДС), работа с первичной документацией, ",
                          "Задачи: ведение ИП, документы, ").replace("Офис в центре города.", "Оформление по ТК РФ.")
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00", phone=""))
    b = one(post(edited, "vk", "club", "2", "2026-10-28T11:00:00+00:00", phone=""))
    sim = similarity(shingles(a._text_norm), shingles(b._text_norm))  # type: ignore[attr-defined]
    assert 0.2 < sim < 0.9
    strict = assign_duplicates([a, b], threshold=min(sim + 0.05, 0.99))
    assert strict[1].duplicate_of is None
    b.duplicate_of = None
    loose = assign_duplicates([a, b], threshold=max(sim - 0.05, 0.05))
    assert loose[1].duplicate_of == 1


def test_default_threshold_value():
    assert 0.3 <= SIMILARITY_THRESHOLD <= 0.8


def test_main_is_earliest_published():
    late = one(post(BASE, "telegram", "c", "1", "2026-10-29T10:00:00+00:00"))
    early = one(post(BASE, "vk", "club", "2", "2026-10-27T10:00:00+00:00"))
    assign_duplicates([late, early], ids=[7, 8])
    assert early.duplicate_of is None and late.duplicate_of == 8


def test_main_tie_goes_to_more_filled_fields():
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post(BASE + " Оплата от 60000 руб. в месяц. Район: Ворошиловский.", "vk", "club", "2", "2026-10-28T10:00:00+00:00"))
    assign_duplicates([a, b], ids=[1, 2])
    assert (a.duplicate_of, b.duplicate_of) == (2, None)


def test_missing_fields_merged_into_main():
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00", phone=""))
    b = one(post(BASE + " Оплата от 60000 руб. в месяц. Ворошиловский район. Тел. 8 949 000 00 05", "vk", "club", "2",
                 "2026-10-28T11:00:00+00:00", phone=""))
    assert a.pay_min is None and a.contacts.get("phones") in (None, [])
    assign_duplicates([a, b])
    assert a.duplicate_of is None and b.duplicate_of == 1
    assert a.pay_min == 60000 and a.pay_period == "месяц"
    assert a.district == "Ворошиловский"
    assert a.contacts["phones"] == ["+79490000005"]


def test_merge_into_does_not_overwrite_existing():
    main = Vacancy(profession="повар", pay_min=5000, pay_max=5000, pay_period="смена", city="Донецк")
    dup = Vacancy(profession="повар", pay_min=9000, pay_max=9000, pay_period="смена", city="Донецк", district="Киевский")
    assert merge_into(main, dup) is True
    assert main.pay_min == 5000 and main.district == "Киевский"
    assert merge_into(main, dup) is False


def test_same_source_posted_twice_is_duplicate():
    a = one(post(BASE, "telegram", "chan", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post(BASE, "telegram", "chan", "2", "2026-10-28T18:00:00+00:00"))
    assign_duplicates([a, b])
    assert b.duplicate_of == 1


def test_same_source_different_ads_with_shared_phone_stay_separate():
    a = one(post("Требуется упаковщик на фабрику. Донецк. Оплата 50000 в месяц. График 5/2.", "telegram", "chan", "1",
                 "2026-10-28T10:00:00+00:00"))
    b = one(post("Требуется упаковщик на фабрику. Донецк. Оплата 50000 в месяц. График 5/2, ночные смены, питание.",
                 "telegram", "chan", "2", "2026-10-28T18:00:00+00:00"))
    c = one(post("Ищем упаковщика, Донецк, 40000 за месяц, склад готовой продукции, работа на конвейере, питание.",
                 "telegram", "chan", "3", "2026-10-28T19:00:00+00:00"))
    assign_duplicates([a, b, c])
    assert c.duplicate_of is None


def test_assign_duplicates_idempotent():
    vs = [
        one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00")),
        one(post(BASE + " Звоните!", "max", "w", "2", "2026-10-28T11:00:00+00:00", phone="8 949 000 00 01")),
        one(post(BASE.upper(), "vk", "club", "3", "2026-10-28T12:00:00+00:00")),
    ]
    assign_duplicates(vs)
    first = [(v.duplicate_of, v.pay_min, v.contacts) for v in vs]
    assign_duplicates(vs)
    assert [(v.duplicate_of, v.pay_min, v.contacts) for v in vs] == first


def test_assign_duplicates_keeps_existing_marks():
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post(BASE, "vk", "club", "2", "2026-10-28T11:00:00+00:00"))
    c = one(post(BASE, "max", "w", "3", "2026-10-27T11:00:00+00:00"))  # самая ранняя, но b уже помечен дублем a
    b.duplicate_of = 1
    assign_duplicates([a, b, c])
    assert a.duplicate_of is None and b.duplicate_of == 1
    assert c.duplicate_of == 1


def test_non_vacancy_status_hidden_is_skipped():
    a = one(post(BASE, "telegram", "c", "1", "2026-10-28T10:00:00+00:00"))
    b = one(post(BASE, "vk", "club", "2", "2026-10-28T11:00:00+00:00"))
    b.status = "hidden"
    assign_duplicates([a, b])
    assert b.duplicate_of is None


# ----------------------------------------------------------------------------- база

@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def fill(conn, raws):
    for r in raws:
        save_raw(conn, r)
    return process_db(conn)


def rows(conn):
    return conn.execute("SELECT id, source_type, source, source_url, duplicate_of FROM vacancies ORDER BY id").fetchall()


def trio():
    return [
        post(BASE, "telegram", "chan", "1", "2026-10-28T10:00:00+00:00"),
        post(BASE.replace("Требуется", "Срочно требуется"), "max", "web", "2", "2026-10-28T12:00:00+00:00", phone="8 949 000 00 01"),
        post(BASE.upper(), "vk", "club1", "3", "2026-10-29T09:00:00+00:00", phone="79490000001"),
    ]


def test_db_dedupe_and_also_published_in(conn):
    stats = fill(conn, trio())
    assert stats["vacancies"] == 3 and stats["duplicates"] == 2
    rs = rows(conn)
    main = next(r for r in rs if r["duplicate_of"] is None)
    assert main["source_type"] == "telegram"
    others = also_published_in(conn, main["id"])
    assert {o["source_type"] for o in others} == {"max", "vk"}
    assert set(others[0]) == {"source_type", "source", "source_url"}
    assert also_published_in(conn, rs[1]["id"]) == []


def test_db_dedupe_idempotent(conn):
    fill(conn, trio())
    before = [tuple(r) for r in rows(conn)]
    assert dedupe(conn) == 0
    assert dedupe(conn) == 0
    assert [tuple(r) for r in rows(conn)] == before


def test_db_dedupe_keeps_manual_marks_and_adds_new(conn):
    fill(conn, trio()[:2])
    first_main = next(r["id"] for r in rows(conn) if r["duplicate_of"] is None)
    save_raw(conn, trio()[2])
    process_db(conn)
    rs = rows(conn)
    assert len(rs) == 3
    assert [r["duplicate_of"] for r in rs].count(None) == 1
    assert next(r["id"] for r in rs if r["duplicate_of"] is None) == first_main


def test_db_merges_missing_fields_into_main(conn):
    a = post(BASE, "telegram", "chan", "1", "2026-10-28T10:00:00+00:00", phone="")
    b = post(BASE + " Оплата от 60000 руб. в месяц. Ворошиловский район.", "vk", "club", "2", "2026-10-28T11:00:00+00:00", phone="")
    fill(conn, [a, b])
    main = conn.execute("SELECT pay_min, district FROM vacancies WHERE duplicate_of IS NULL").fetchone()
    assert (main["pay_min"], main["district"]) == (60000, "Ворошиловский")


def test_db_different_professions_stay_separate(conn):
    a = post("Требуется повар в кафе. Донецк. Оплата 4000 за смену.", "telegram", "c", "1", "2026-10-28T10:00:00+00:00")
    b = post("Требуется официант в кафе. Донецк. Оплата 3500 за смену.", "telegram", "c", "2", "2026-10-28T10:05:00+00:00")
    stats = fill(conn, [a, b])
    assert stats["duplicates"] == 0


def test_db_dedupe_works_without_raw_text(conn):
    v1 = Vacancy(profession="повар", source_type="telegram", source="a", source_url="u1", external_id="1", kind="vacancy",
                 summary="Требуется повар в кафе", city="Донецк", published_at="2026-10-28T10:00:00+00:00",
                 contacts={"phones": ["+79490000001"]})
    v2 = Vacancy(profession="повар", source_type="vk", source="b", source_url="u2", external_id="2", kind="vacancy",
                 summary="Требуется повар в кафе", city="Донецк", published_at="2026-10-28T11:00:00+00:00",
                 contacts={"phones": ["+7 (949) 000-00-01"]})
    save_vacancy(conn, v1)
    save_vacancy(conn, v2)
    assert dedupe(conn) == 1
