"""Вакансии hh.ru: должность берётся из названия вакансии, а отрасль и формат не путаются из-за пунктов условий."""
import json
from pathlib import Path

import pytest

from rabota.models import RawPost
from rabota.pipeline.fields import parse_work_format
from rabota.pipeline.normalize import detect_context_category
from rabota.pipeline.run import process
from rabota.pipeline.titles import clean_title_phrase

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hh"


def _fixture_post() -> RawPost:
    data = json.loads((FIXTURES / "vacancy_prohodchik_vorkuta.json").read_text(encoding="utf-8"))
    return RawPost(**data)


def _hh(name: str, body: str = "", extra: dict | None = None, pay: str = "Зарплата: от 50000 руб. в месяц") -> RawPost:
    text = f"{name}\nРаботодатель: ООО Тест\n{pay}\nГород: Донецк\n{body}"
    return RawPost("hh", "hh.ru", "t1", "https://hh.ru/vacancy/1", text, extra={"name": name, **(extra or {})})


def test_prohodchik_is_not_employee_medicine_remote():
    (v,) = process(_fixture_post())
    assert v.profession == "проходчик"
    assert v.category == "производство"
    assert v.work_format == "вахта"
    assert v.employment == "постоянно"
    assert (v.pay_min, v.pay_max) == (230000, 300000)


def test_unknown_title_from_hh_is_kept_not_replaced_by_employee():
    raw = _hh("Горнорабочий очистного забоя 5 разряда (г. Воркута)")
    (v,) = process(raw)
    assert v.profession != "сотрудник"
    assert v.profession.startswith("горнорабочий")
    assert "воркута" not in v.profession and "разряд" not in v.profession


@pytest.mark.parametrize(
    "name",
    [
        "Территориальный представитель по торговому маркетингу / TSM",
        "Агент по привлечению клиентов на импорт автомобилей",
        "Оператор станков с числовым программным управлением ЧПУ (токарь-фрезеровщик)",
    ],
)
def test_hh_never_falls_back_to_employee(name):
    (v,) = process(_hh(name))
    assert v.profession not in ("сотрудник", "работник")
    assert v.title_raw == name


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Проходчик 5 разряда (г. Воркута)", "проходчик"),
        ("Старший садовник (г. Ялта, Республика Крым)", "старший садовник"),
        ("Хирург-Проктолог/Колопроктолог Мариуполь (г.Мариуполь / ДНР)", "хирург-проктолог/колопроктолог"),
        ("Разнорабочий, г. Москва", "разнорабочий"),
        ("Монтажник ЖБК, Саратов", "монтажник жбк"),
        ("Менеджер по продажам оборудования", "менеджер по продажам оборудования"),
    ],
)
def test_clean_title_phrase(raw, clean):
    assert clean_title_phrase(raw) == clean


def test_training_courses_do_not_make_job_remote():
    text = "Формат работы: на месте работодателя\nУсловия:\n- дистанционные курсы и внешние программы обучения."
    assert parse_work_format(text) != "удалённо"


def test_real_remote_job_still_remote():
    assert parse_work_format("Формат работы: удалённо\nПродажи по телефону") == "удалённо"
    assert parse_work_format("Работа дистанционно, оплата сдельная") == "удалённо"
    assert parse_work_format("Работа из дома, оплата сдельная") == "удалённо"


def test_explicit_on_site_beats_stray_remote_word():
    text = "Формат работы: на месте работодателя\nОбщение с коллегами в том числе удалённо"
    assert parse_work_format(text) != "удалённо"


def test_hh_structured_remote_format_wins():
    extra = {"workFormats": [{"workFormatsElement": ["REMOTE"]}]}
    (v,) = process(_hh("Бухгалтер", "Формат работы: удалённо", extra))
    assert v.work_format == "удалённо"


def test_hh_structured_on_site_beats_text_remote():
    extra = {"workFormats": [{"workFormatsElement": ["ON_SITE"]}]}
    (v,) = process(_hh("Кладовщик", "Условия:\n- дистанционное обучение", extra))
    assert v.work_format != "удалённо"


def test_hh_fly_in_fly_out_is_vahta():
    extra = {"@workSchedule": "flyInFlyOut", "employmentForm": "FLY_IN_FLY_OUT"}
    (v,) = process(_hh("Электрик", "", extra))
    assert v.work_format == "вахта"


def test_conditions_section_does_not_set_category():
    body = "Обязанности:\n- Бурить шпуры.\nУсловия:\n- Компенсация расходов на медицинский осмотр.\n- Столовая и кафе."
    assert detect_context_category(body) is None


def test_duties_still_set_category():
    assert detect_context_category("Требуется помощник в клинику\nУсловия:\n- график 2/2") == "медицина"


def test_it_head_is_office_not_transport():
    body = "Обязанности:\n- Руководить отделом.\nУсловия:\n- Перевозка сотрудников служебным транспортом."
    (v,) = process(_hh("Начальник отдела по информационным технологиям", body))
    assert v.category == "офис"


def test_street_in_address_line_is_not_a_profession():
    raw = _hh("Менеджер по работе с ключевыми клиентами (КАМ)", "Адрес: Донецк, улица Проходчиков, 20\nОбязанности:\n- Вести клиентов.")
    (v,) = process(raw)
    assert v.profession != "проходчик"
    assert v.category != "производство"


def test_employer_line_is_not_a_profession():
    raw = _hh("Помощник руководителя", "Работодатель: Пекарня Хлебная\nОбязанности:\n- Вести документы.")
    (v,) = process(raw)
    assert v.profession != "пекарь"


def test_hiring_phrase_title_is_not_a_profession():
    (v,) = process(_hh("Требуются сотрудники на оптово-розничную строительную базу"))
    assert v.profession in ("сотрудник", "работник")


def test_city_tail_removed_from_unknown_title_in_text_path():
    raw = RawPost(
        "hh", "hh.ru", "t2", "https://hh.ru/vacancy/2",
        "Хирург-Проктолог/Колопроктолог Мариуполь (г.Мариуполь / ДНР)\nЗарплата: от 300000 руб. в месяц\nУсловия:\n- ДМС.",
        extra={"name": "Хирург-Проктолог/Колопроктолог Мариуполь (г.Мариуполь / ДНР)"},
    )
    (v,) = process(raw)
    assert "мариуполь" not in v.profession and "(" not in v.profession
    assert v.category == "медицина"


def test_building_titles_are_construction():
    (v,) = process(_hh("Начальник строительного участка", "Условия:\n- Компенсация медицинского осмотра."))
    assert v.category == "стройка"


def test_forbidden_remote_in_description_means_not_remote():
    extra = {"workFormats": [{"workFormatsElement": ["ON_SITE"]}], "@workSchedule": "fullDay"}
    (v,) = process(_hh("Бухгалтер", "Формат работы: на месте работодателя\nУдаленно не рассматриваем!", extra))
    assert v.work_format != "удалённо"


def test_search_window_not_shifted_by_card_lines():
    # "рабочий день" из описания не должен делать из должности "рабочий"
    raw = _hh("Сотрудник фаст-фуда", "Занятость: полная занятость\nГрафик: 2/2\nГород: Донецк\nАдрес: Донецк, ул. Артема, 1\nОписание:\nРабочий день с 9 до 18.")
    (v,) = process(raw)
    assert v.profession == "сотрудник фаст-фуда"
