"""Правила анкеты: проверка полей, навыки, заполненность, имя в сокращении."""
import pytest

from rabota.resumes import rules
from tests.resumes.conftest import clean_of, make_form


def validate(**over):
    return rules.validate(make_form(**over))


def test_valid_form_is_cleaned():
    clean = clean_of()
    assert clean["profession"] == "Бариста" and clean["pay_min"] == 30000 and clean["pay_period"] == "месяц"
    assert clean["work_formats"] == ["точка"] and clean["employments"] == ["постоянно"]
    assert clean["contact_phone"] == "+79491112233" and clean["contact_telegram"] == "maria_p"
    assert clean["skills"] == ["кофе", "касса", "сервис"] and clean["experience_years"] == 3
    assert clean["places"] == [{"place": "Кофейня Зерно", "role": "Бариста", "period": "2022-2024"}]
    assert clean["visibility"] == "public" and clean["consent"] is True


def test_profession_is_required_and_has_no_links():
    assert "profession" in validate(profession="")[1]
    assert "profession" in validate(profession="ab")[1]
    assert "profession" in validate(profession="12345")[1]
    assert "profession" in validate(profession="бариста http://spam.ru")[1]


@pytest.mark.parametrize("field,value", [("pay_min", "99999999999"), ("experience_years", "99")])
def test_numbers_are_limited(field, value):
    assert field in validate(**{field: value})[1]


def test_pay_needs_period_and_period_must_be_known():
    assert "pay_period" in validate(pay_period="")[1]
    assert "pay_period" in validate(pay_period="год")[1]
    assert not validate(pay_min="", pay_period="")[1]


def test_unknown_formats_and_employments_are_dropped():
    clean, errors = validate(work_formats=["точка", "космос"], employments=["постоянно", "вечно"])
    assert not errors and clean["work_formats"] == ["точка"] and clean["employments"] == ["постоянно"]


def test_about_limit_and_links():
    assert rules.validate(make_form(about="я" * rules.MAX_ABOUT))[1] == {}
    assert "about" in validate(about="я" * (rules.MAX_ABOUT + 1))[1]
    assert "about" in validate(about="пишите на https://spam.example")[1]
    assert "about" in validate(about="мой сайт spam.ru")[1]


def test_skills_are_split_deduplicated_and_limited():
    assert rules.parse_skills("кофе, Кофе; касса\nсервис,,") == ["кофе", "касса", "сервис"]
    assert "skills" in validate(skills=", ".join(f"навык{i}" for i in range(rules.MAX_SKILLS + 1)))[1]
    assert "skills" in validate(skills="я" * (rules.MAX_SKILL_LEN + 1))[1]
    assert "skills" in validate(skills="сайт http://x.ru")[1]


def test_places_need_name_or_role_and_no_links():
    assert "place_1" in validate(place_1="", role_1="", period_1="2020")[1]
    assert "place_1" in validate(place_1="https://spam.example")[1]
    assert rules.validate(make_form(place_1="", role_1="", period_1="", place_2="Кафе", role_2=""))[0]["places"] == [
        {"place": "Кафе", "role": "", "period": ""}]


def test_contacts_are_checked():
    assert "contact_phone" in validate(contact_phone="123")[1]
    assert "contact_telegram" in validate(contact_telegram="@a")[1]
    assert validate(contact_phone="", contact_telegram="")[1] == {}


def test_visibility_must_be_known_and_open_needs_consent():
    assert "visibility" in validate(visibility="all")[1]
    assert "consent" in validate(consent=None)[1]
    assert "consent" in validate(visibility="open", consent=None)[1]
    assert validate(visibility="hidden", consent=None)[1] == {}


def test_consent_error_text_differs_when_already_given():
    first = rules.validate(make_form(consent=None), consented_now=False)[1]["consent"]
    again = rules.validate(make_form(consent=None), consented_now=True)[1]["consent"]
    assert first != again


def test_short_name_hides_surname():
    assert rules.short_name("Мария Петрова") == "Мария П."
    assert rules.short_name("Мария") == "Мария"
    assert rules.short_name("") == "Соискатель"
    assert rules.short_name("Анна Мария ван дер Берг") == "Анна Б."


def test_completeness_full_and_empty():
    full = {"profession": "бариста", "pay_min": 1, "district": "Ворошиловский", "work_formats": ["точка"], "experience_years": 2,
            "places": [{"place": "а", "role": "", "period": ""}], "skills": ["а", "б", "в"], "about": "я" * 100,
            "contact_phone": "+79490000000"}
    assert rules.completeness(full) == (100, [])
    percent, tips = rules.completeness({"profession": "бариста"})
    assert percent == 15 and tips[0]["points"] >= tips[-1]["points"] and len(tips) == 8


def test_completeness_zero_experience_counts():
    base = {"profession": "бариста", "experience_years": 0}
    assert rules.completeness(base)[0] == 25


def test_consent_version_follows_legal_date(monkeypatch):
    monkeypatch.delenv("LEGAL_UPDATED_AT", raising=False)
    assert rules.consent_version().startswith("resume ")
    monkeypatch.setenv("LEGAL_UPDATED_AT", "01.01.2031")
    assert rules.consent_version() == "resume 01.01.2031"


def test_profession_suggestions_come_from_dictionary():
    assert "бариста" in rules.PROFESSION_SUGGESTIONS and len(rules.PROFESSION_SUGGESTIONS) > 100
