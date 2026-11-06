"""Слова "девушка", "парень" в объявлении не должность: "девушка в офис" это офисный работник."""
import pytest

from rabota.pipeline.titles import pick_profession


@pytest.mark.parametrize(
    "text",
    [
        "Требуется девушка в офис в Донецке - зарплата 100000р и бонусы.",
        "Девушка в офис. Донецк, 100000",
        "Нужен парень в офис",
        "Ищем женщину в офис, график 5/2",
    ],
)
def test_person_in_office_is_office_worker(text):
    choice = pick_profession(text)
    assert choice is not None
    assert choice.lemma == "офисный работник"
    assert choice.category == "офис"


def test_person_word_is_not_a_profession():
    choice = pick_profession("Требуется девушка, зарплата 50000")
    assert choice is None or choice.lemma not in ("девушка", "женщина", "парень", "мужчина")


def test_girl_administrator_keeps_real_profession():
    choice = pick_profession("Требуется девушка-администратор в салон")
    assert choice is not None and choice.lemma == "администратор"
