"""Поля вакансии: оплата, график, город, район, опыт, возраст, контакты, подозрительное, словарь профессий."""
import pytest

from rabota.pipeline.contacts import (
    extract_contacts,
    extract_email,
    extract_max_phone,
    extract_phones,
    extract_telegram,
)
from rabota.pipeline.dictionaries import find_professions, profession_category
from rabota.pipeline.fields import (
    detect_suspicious,
    parse_age_min,
    parse_city,
    parse_district,
    parse_employment,
    parse_experience,
    parse_requirements,
    parse_schedule,
)
from rabota.pipeline.pay import mentions_negotiable_pay, parse_pay

# ----------------------------------------------------------------------------- оплата


@pytest.mark.parametrize(("text", "lo", "hi", "period"), [
    ("4000р. смена", 4000, 4000, "смена"),
    ("Оплата 3 500 руб за смену", 3500, 3500, "смена"),
    ("от 48 500 - 56 000 ₽", 48500, 56000, "месяц"),
    ("1250 ₽ каждому за задачу", 1250, 1250, "задача"),
    ("100 000 - 100 000 ₽", 100000, 100000, "месяц"),
    ("от 80 до 100 тыс.", 80000, 100000, "месяц"),
    ("до 200 000 ₽", None, 200000, "месяц"),
    ("от 70000 руб. в месяц", 70000, None, "месяц"),
    ("5,000 руб. в день", 5000, 5000, "день"),
    ("З/П от 50'000 рублей", 50000, None, "месяц"),
    ("Оплата 4000 в день", 4000, 4000, "день"),
    ("150 руб/час", 150, 150, "час"),
    ("з/п 90к", 90000, 90000, "месяц"),
    ("Зарплата: от 13547 руб.", 13547, None, "месяц"),
])
def test_parse_pay(text, lo, hi, period):
    p = parse_pay(text)
    assert (p.pay_min, p.pay_max, p.pay_period) == (lo, hi, period)


@pytest.mark.parametrize("text", [
    "по договорённости", "Зарплата по договоренности", "оплата обсуждается на собеседовании", "Звоните +79490000000",
    "График 5/2, с 9:00 до 18:00", "Опыт от 2 лет, возраст 25 лет",
])
def test_parse_pay_empty(text):
    p = parse_pay(text)
    assert not p.has_amount
    assert p.pay_period is None


def test_pay_ignores_bonus_and_percent():
    p = parse_pay("Оплата 4000 за смену + премия 1000, суточные 500, 10% от продаж")
    assert (p.pay_min, p.pay_max) == (4000, 4000)


def test_pay_daily_flag():
    assert parse_pay("Оплата 4000 за смену, выплата ежедневно").pay_daily
    assert not parse_pay("Оплата 4000 за смену, аванс и зарплата").pay_daily


def test_pay_monthly_estimate_from_schedule():
    p = parse_pay("Оплата 4000 за смену", schedule="2/2")
    assert p.pay_monthly_estimate == 60000
    p = parse_pay("от 70000 руб. в месяц")
    assert p.pay_monthly_estimate == 70000


def test_mentions_negotiable_pay():
    assert mentions_negotiable_pay("Зарплата по договоренности")
    assert not mentions_negotiable_pay("Зарплата 40000")


# ----------------------------------------------------------------------------- график, город, район, опыт, возраст

@pytest.mark.parametrize(("text", "expected"), [
    ("график 2/2 с 9:00 до 18:00", "2/2"),
    ("График: 5/2", "5/2"),
    ("сутки через трое", "сутки"),
    ("график пн-пт", "5/2"),
    ("свободный график", "свободный"),
    ("7 через 7", "7/7"),
    ("Оплата 4000", None),
])
def test_parse_schedule(text, expected):
    assert parse_schedule(text) == expected


def test_schedule_ratio_not_taken_from_date_or_pay():
    assert parse_schedule("Оплата 3/4 от суммы, 12/10/2026") is None


@pytest.mark.parametrize(("text", "city"), [
    ("г. Макеевка, ул. Ленина 1", "Макеевка"),
    ("Работа в Донецке", "Донецк"),
    ("Мариуполь, вахта", "Мариуполь"),
    ("город Горловка", "Горловка"),
    ("Шахтёрск", "Шахтёрск"),
    ("Работа в офисе, звоните", None),
])
def test_parse_city(text, city):
    assert parse_city(text) == city


@pytest.mark.parametrize(("text", "district"), [
    ("Ворошиловский район", "Ворошиловский"),
    ("Куйбышевский р-н", "Куйбышевский"),
    ("в Буденновском районе", "Буденновский"),
    ("мкр. Текстильщик", "Текстильщик"),
    ("ул. Ленина 5", None),
])
def test_parse_district(text, district):
    assert parse_district(text) == district


@pytest.mark.parametrize(("text", "expected"), [
    ("без опыта", "не нужен"),
    ("опыт не требуется", "не нужен"),
    ("опыт от 1 года", "от 1 года"),
    ("Опыт работы от 1 до 3 лет", "от 1 года"),
    ("Требования: опыт работы от 2 лет", "от 2 лет"),
    ("Оплата 4000", None),
])
def test_parse_experience(text, expected):
    assert parse_experience(text) == expected


@pytest.mark.parametrize(("text", "age"), [
    ("от 18 лет", 18), ("возраст от 21 года", 21), ("Совершеннолетние", 18), ("опыт 5 лет", None),
])
def test_parse_age_min(text, age):
    assert parse_age_min(text) == age


@pytest.mark.parametrize(("text", "expected"), [
    ("подработка", "подработка"),
    ("на сегодня, срочно, выход завтра", "на сегодня"),
    ("постоянная работа, официальное трудоустройство", "постоянно"),
])
def test_parse_employment(text, expected):
    assert parse_employment(text) == expected


def test_parse_requirements_section():
    text = "Обязанности: продажи\nТребования: опыт от 1 года, грамотная речь\nУсловия: график 5/2"
    got = parse_requirements(text)
    assert got and "грамотная речь" in got and "график" not in got


# ----------------------------------------------------------------------------- контакты

def test_phone_formats_collapse_to_one():
    text = "+7 (949) 000-00-00, 8 949 000 00 00, 79490000000, 8-949-000-00-00"
    assert extract_phones(text) == ["+79490000000"]


def test_phones_several_keep_order():
    assert extract_phones("Звоните +79490000001 или 89490000002") == ["+79490000001", "+79490000002"]


def test_phone_ignores_pay_and_dates():
    assert extract_phones("Оплата 120 000 руб, дата 29.10.2026, график 2/2") == []


def test_telegram_handles():
    assert extract_telegram("Пишите @user, подробности в @some_channel", "some_channel") == ["@user"]
    assert extract_telegram("Контакт: t.me/user_name", "x") == ["@user_name"]


def test_telegram_ignores_email_domain():
    assert extract_telegram("Резюме: hr@example.com", "x") == []


def test_email():
    assert extract_email("Резюме: hr@example.com") == "hr@example.com"
    assert extract_email("нет почты") is None


def test_max_phone_by_proximity():
    text = "Звонить +79490000001\nНаписать в MAX: +79490000002"
    phones = extract_phones(text)
    assert extract_max_phone(text, phones) == "+79490000002"


def test_extract_contacts_dict():
    c = extract_contacts("Тел. +79490000000, tg @user, mail hr@example.com", "src")
    assert c["phones"] == ["+79490000000"]
    assert c["telegram"] == "@user"
    assert c["email"] == "hr@example.com"


# ----------------------------------------------------------------------------- подозрительное

@pytest.mark.parametrize("text", [
    "Оплата за регистрацию 3000, предоплата за форму",
    "Нужны люди без вложений, заработок от 7000 в день",
    "Работа курьером, залог за сумку 2000",
])
def test_suspicious_texts(text):
    assert detect_suspicious(text, None, None)


def test_suspicious_by_pay_level():
    assert detect_suspicious("Работа в офисе", 20000, "день")
    assert detect_suspicious("Работа в офисе", 800000, "месяц")
    assert not detect_suspicious("Работа в офисе", 60000, "месяц")


def test_not_suspicious_normal():
    assert not detect_suspicious("Требуется продавец, график 2/2, оплата 4000 за смену", 4000, "смена")


# ----------------------------------------------------------------------------- словарь профессий

@pytest.mark.parametrize(("text", "lemma"), [
    ("требуются повара", "повар"),
    ("нужны поварам", "повар"),
    ("ищем продавца-консультанта", "продавец"),
    ("кассиру на постоянную работу", "кассир"),
    ("в команду бухгалтеров", "бухгалтер"),
    ("нужна официантка", "официант"),
    ("ищем бариста", "бариста"),
    ("требуется водитель категории С", "водитель"),
    ("Нужен оператор 1С", "оператор 1с"),
    ("помощник повара", "помощник повара"),
    ("мойщица посуды", "мойщик посуды"),
    ("медицинская сестра", "медсестра"),
    ("Автослесарь", "автослесарь"),
])
def test_dictionary_declensions(text, lemma):
    assert lemma in [h.lemma for h in find_professions(text)]


def test_dictionary_prefers_longest_match():
    lemmas = [h.lemma for h in find_professions("требуется помощник повара")]
    assert lemmas == ["помощник повара"]


def test_dictionary_no_false_hits():
    assert find_professions("оплата за смену, график 2/2, Донецк") == []


def test_dictionary_size_and_categories():
    from rabota.models import CATEGORIES
    from rabota.pipeline.dictionaries import PROFESSION_SOURCE

    lines = [ln for ln in PROFESSION_SOURCE.splitlines() if "=" in ln]
    assert 150 <= len(lines) <= 400
    for lemma in ("повар", "водитель", "бухгалтер", "грузчик", "медсестра"):
        assert profession_category(lemma) in CATEGORIES
    assert profession_category("повар") == "общепит"
    assert profession_category("грузчик") == "склад"
