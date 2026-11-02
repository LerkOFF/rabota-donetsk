"""Нормализация названий работодателей и отсев мусора для автокарточек."""
from __future__ import annotations

import pytest

from rabota.employers_auto import names


@pytest.mark.parametrize("raw", ["ООО «МКД-Трейд»", "ооо  мкд-трейд", 'ООО "МКД Трейд"', "  ООО  «МКД\u2014Трейд»  "])
def test_same_company_gets_same_norm(raw):
    d = names.decide(raw)
    assert d.ok and d.info.norm == "мкд трейд"
    assert d.info.legal_form == "ООО"


def test_legal_form_is_kept_and_not_in_norm():
    for raw, form in [("АО \"Донбасс\"", "АО"), ("ЗАО Заря", "ЗАО"), ("ГУП ДНР «Вода Донбасса»", "ГУП"), ("МУП Горсвет", "МУП")]:
        d = names.decide(raw)
        assert d.ok
        assert d.info.legal_form == form
        assert form.lower() not in d.info.norm.split()


def test_yo_and_latin_homoglyphs_fold():
    assert names.decide("Магазин «Ёлочка»").info.norm == names.decide("магазин елочка").info.norm
    # латинская "a" внутри кириллического слова
    assert names.decide("Рaстишка").info.display == "Растишка"
    assert names.decide("Pastiano").info.norm == "pastiano"


@pytest.mark.parametrize("raw,reason", [
    ("", "empty"), ("\u2014", "empty"), ("не указано", "placeholder"), ("Аб", "short"),
    ("Иван Иванов", "person"), ("ИП Иванов Иван Иванович", "person"), ("Мария", "person"),
    ("Бариста", "profession"), ("Кафе", "generic"), ("@ivan_petrov", "garbage"),
])
def test_rejects_non_employers(raw, reason):
    d = names.decide(raw)
    assert not d.ok and d.reason == reason


def test_profession_is_not_a_company():
    assert names.decide("Повар", "повар").reason == "profession"


def test_telegram_contact_is_not_company():
    assert not names.decide("@kafe_donetsk", source_type="telegram").ok


def test_real_brand_passes():
    assert names.decide("Pastiano").ok
    assert names.decide("Кофейня Зерно").ok


def test_city_key_and_fold():
    assert names.city_key("Донецк") == "донецк"
    assert names.fold("Ёлка") == "елка"
