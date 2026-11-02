"""Фильтры подписки: нормализация, название, ссылка на выдачу."""
from rabota.alerts import filters
from rabota.web import format as fmt


def test_normalize_keeps_only_set_filters_and_known_keys():
    data = filters.normalize({"q": " Бариста  ", "city": "Донецк", "sort": "pay", "page": "3", "daily": "1", "adult": "", "x": "y"})
    assert data == {"q": "бариста", "city": "Донецк", "daily": True}


def test_pay_from_is_parsed_like_the_feed():
    assert filters.normalize({"pay_from": "30 000 ₽"}) == {"pay_from": 30000}


def test_no_conditions_means_no_subscription():
    assert filters.has_conditions({}) is False
    assert filters.has_conditions({"sort": "pay"}) is False
    assert filters.has_conditions({"q": "кассир"}) is True
    assert filters.has_conditions({"side_job": True}) is True


def test_key_is_stable_for_the_same_search():
    a = filters.key_of({"q": "бариста", "city": "Донецк"})
    b = filters.key_of({"city": "Донецк", "q": " бариста ", "sort": "pay"})
    assert a == b
    assert a != filters.key_of({"q": "бариста", "city": "Макеевка"})


def test_title_examples():
    assert filters.describe({"q": "Бариста", "city": "Донецк"}) == "бариста в Донецке"
    assert filters.describe({"q": "кассир"}) == "кассир в Донецке и ДНР"
    assert filters.describe({"side_job": True, "pay_from": 30000, "city": "Макеевка"}) == " ".join(f"подработка, от {fmt.money(30000)} ₽ в Макеевке".split())
    assert filters.describe({"category": "склад", "district": "Ворошиловский"}) == "склад в районе Ворошиловский"
    assert filters.describe({"q": "грузчик", "daily": True, "no_experience": True}).startswith("грузчик, выплата каждый день, без опыта")


def test_title_is_limited():
    assert len(filters.describe({"q": "я" * 80, "city": "Донецк", "side_job": True, "daily": True, "pay_from": 1})) <= filters.TITLE_MAX


def test_in_city_known_and_fallback():
    assert filters.in_city("Донецк") == "в Донецке"
    assert filters.in_city("Ясиноватая") == "в Ясиноватой"
    assert filters.in_city("Макеевка") == "в Макеевке"
    assert filters.in_city("Новогродовка") == "в Новогродовке"
    assert filters.in_city("") == ""


def test_vacancies_url_repeats_the_feed_query():
    url = filters.vacancies_url("https://example.test", {"q": "бариста", "city": "Донецк", "daily": True})
    assert url == "https://example.test/vacancies?q=%D0%B1%D0%B0%D1%80%D0%B8%D1%81%D1%82%D0%B0&city=%D0%94%D0%BE%D0%BD%D0%B5%D1%86%D0%BA&daily=1"
    assert filters.vacancies_url("https://example.test", {}) == "https://example.test/vacancies"
