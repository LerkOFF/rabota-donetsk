from datetime import UTC, datetime, timedelta

import pytest

from rabota.web import format as fmt

NB = " "
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lo", "hi", "period", "text"),
    [
        (4000, 4000, "смена", f"4{NB}000{NB}₽ за смену"),
        (4000, None, "смена", f"от{NB}4{NB}000{NB}₽ за смену"),
        (None, 5000, "день", f"до{NB}5{NB}000{NB}₽ в день"),
        (48500, 56000, "месяц", f"от{NB}48{NB}500 до{NB}56{NB}000{NB}₽ в месяц"),
        (56000, 48500, "месяц", f"от{NB}48{NB}500 до{NB}56{NB}000{NB}₽ в месяц"),
        (1500, 1500, "час", f"1{NB}500{NB}₽ в час"),
        (30000, 30000, "задача", f"30{NB}000{NB}₽ за задачу"),
    ],
)
def test_pay_text_has_amount_and_period(lo, hi, period, text):
    assert fmt.pay_parts(lo, hi, period)["text"] == text


def test_pay_unknown_is_honest():
    p = fmt.pay_parts(None, None, None)
    assert p["known"] is False and p["main"] == "Оплата по договорённости" and p["period"] == ""


def test_pay_parts_split_for_card():
    p = fmt.pay_parts(4000, 4000, "смена")
    assert p["main"] == f"4{NB}000{NB}₽" and p["period"] == "за смену" and p["known"]


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "вакансия"), (2, "вакансии"), (4, "вакансии"), (5, "вакансий"), (11, "вакансий"), (21, "вакансия"), (112, "вакансий")],
)
def test_plural(n, word):
    assert fmt.plural(n, "вакансия", "вакансии", "вакансий") == word


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=10), "только что"),
        (timedelta(minutes=1), "1 минуту назад"),
        (timedelta(minutes=5), "5 минут назад"),
        (timedelta(hours=2), "2 часа назад"),
        (timedelta(hours=1), "1 час назад"),
        (timedelta(hours=21), "21 час назад"),
        (timedelta(days=1), "вчера"),
        (timedelta(days=3), "3 дня назад"),
        (timedelta(days=17), "12 октября"),
        (timedelta(days=400), "24 сентября 2025"),
    ],
)
def test_time_ago(delta, text):
    assert fmt.time_ago((NOW - delta).isoformat(), NOW) == text


def test_time_ago_handles_garbage_and_naive_dates():
    assert fmt.time_ago(None, NOW) == ""
    assert fmt.time_ago("не дата", NOW) == ""
    assert fmt.time_ago("2026-10-29 10:00:00", NOW) == "2 часа назад"
    assert fmt.time_ago("2026-10-29T10:00:00Z", NOW) == "2 часа назад"


def test_location_and_district():
    assert fmt.district_label("Ворошиловский") == "Ворошиловский район"
    assert fmt.district_label("Текстильщик") == "Текстильщик"
    assert fmt.district_label("Макеевский район") == "Макеевский район"
    assert fmt.location_label("Донецк", "Ворошиловский") == "Донецк, Ворошиловский район"
    assert fmt.location_label("Донецк", None) == "Донецк"
    assert fmt.location_label(None, None) == ""


def test_phones():
    assert fmt.phone_pretty("+79490000000") == "+7 949 000-00-00"
    assert fmt.phone_href("8 (949) 000-00-00") == "+79490000000"
    assert fmt.normalize_phone("8 949 000 00 00") == "+79490000000"
    assert fmt.normalize_phone("949 000-00-00") == "+79490000000"
    assert fmt.normalize_phone("12345") is None
    assert fmt.normalize_phone("") is None


def test_telegram_href():
    assert fmt.telegram_href("@name") == "https://t.me/name"
    assert fmt.telegram_href("https://t.me/x") == "https://t.me/x"
