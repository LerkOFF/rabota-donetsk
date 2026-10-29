"""Структурные поля источников (Работа России, hh.ru, ЦЗН) важнее разбора текста."""
from rabota.models import RawPost
from rabota.pipeline.normalize import extra_text, find_extra
from rabota.pipeline.run import process


def test_trudvsem_fields_from_extra():
    extra = {
        "job-name": "Уборщик производственных и служебных помещений",
        "salary_min": 27093, "salary_max": 27093, "salary": "от 27093",
        "company": {"name": "ГБДОУ \"ДЕТСКИЙ САД № 2 ШАХТЕРСКОГО М.О.\" ДНР"},
        "employment": "Полная занятость", "schedule": "Полный рабочий день",
        "requirements": "коммуникабельность", "duty": "дезинфицирование туалетов",
        "addresses": {"address": [{"location": "Донецкая Народная Республика, г. Шахтёрск, ул. Ленина"}]},
    }
    raw = RawPost("trudvsem", "trudvsem", "a1", "https://example.com/a1", "", extra=extra)
    (v,) = process(raw)
    assert v.profession == "уборщик"
    assert (v.pay_min, v.pay_period) == (27093, "месяц")
    assert v.city == "Шахтёрск"
    assert v.employment == "постоянно"
    assert "ДЕТСКИЙ" not in (v.employer or "")


def test_hh_compensation_from_extra():
    extra = {
        "name": "Менеджер по продажам",
        "company": {"name": "Банк", "visibleName": "Банк, Старт карьеры"},
        "compensation": {"from": 70000, "to": 90000, "currencyCode": "RUR", "mode": "MONTH"},
        "address": {"city": "Старобешево"},
        "workSchedule": "fullDay",
    }
    raw = RawPost("hh", "hh.ru", "h1", "https://example.com/h1", "Менеджер по продажам\nРаботодатель: Банк", extra=extra)
    (v,) = process(raw)
    assert v.profession == "менеджер по продажам"
    assert (v.pay_min, v.pay_max, v.pay_period) == (70000, 90000, "месяц")
    assert v.city == "Старобешево"


def test_mczn_fields_from_extra():
    extra = {
        "salary_min": 27093, "salary_max": 35000, "employer": "ГБУСО ЦСОН КИРОВСКОГО РАЙОНА ГОРОДА МАКЕЕВКИ",
        "city": "г. Макеевка", "schedule": "Полный рабочий день", "experience": "Без опыта",
        "title": "Специалист по охране труда", "description": "Организация обучения по охране труда",
    }
    raw = RawPost("mczn", "m-czn.ru", "m1", "https://example.com/m1", "", extra=extra)
    (v,) = process(raw)
    assert v.profession == "специалист по охране труда"
    assert (v.pay_min, v.pay_max) == (27093, 35000)
    assert v.city == "Макеевка"
    assert v.experience == "не нужен"


def test_structured_extra_beats_text():
    extra = {"job-name": "Повар", "salary_min": 40000, "salary_max": 50000}
    raw = RawPost("trudvsem", "trudvsem", "a2", "u", "Повар\nЗарплата: 99999 руб.", extra=extra)
    (v,) = process(raw)
    assert (v.pay_min, v.pay_max) == (40000, 50000)


def test_find_extra_nested_and_normalized_keys():
    extra = {"data": {"Job_Name": "Повар"}, "x": [{"salary-min": 1}]}
    assert find_extra(extra, "job-name") == "Повар"
    assert find_extra(extra, "nothing") is None


def test_extra_text_used_when_text_is_empty():
    assert "Повар" in extra_text({"job-name": "Повар", "salary_min": 40000})
