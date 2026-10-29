"""Тесты сборщика Мой ЦЗН: разбор сохранённых страниц списка и вакансии. Без сети."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import mczn
from rabota.models import SOURCE_TYPES, RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mczn"
LIST_URL = "https://m-czn.ru/vacancy/dnr"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(mczn.time, "sleep", lambda _s: None)


def test_contract():
    assert mczn.NAME == "mczn"
    assert mczn.SOURCES == ("mczn",)
    assert "mczn" in SOURCE_TYPES


def test_parse_list_cards():
    items = mczn.parse_list(read("list.html"))
    assert [i["id"] for i in items] == ["2977183", "2977178", "2977179", "2977180", "2977181"]
    first = items[0]
    assert first["title"] == "Специалист по охране труда"
    assert first["url"] == "https://m-czn.ru/vacancy/g-makeevka/spetsialist-2977183"
    assert first["salary_min"] == 27093
    assert first["salary_max"] == 35000
    assert first["employer"] == "ГБУСО ЦСОН КИРОВСКОГО РАЙОНА ГОРОДА МАКЕЕВКИ"
    assert first["city"] == "г. Макеевка"
    assert first["schedule"] == "Полный рабочий день"
    assert first["experience"] == "Без опыта"
    assert first["updated"] == "2026-10-29T00:00:00+00:00"


def test_parse_list_empty_page():
    assert mczn.parse_list("<html><body>пусто</body></html>") == []


def test_parse_detail():
    d = mczn.parse_detail(read("detail.html"))
    assert d["description"].startswith("Планирование и организация обучения")
    assert d["fields"]["Предприятие"].startswith("ГБУСО ЦСОН")
    assert "Чудская улица" in d["fields"]["Адрес"]
    assert d["fields"]["Сфера деятельности"] == "Здравоохранение и социальное обеспечение"
    assert d["fields"]["Образование"] == "Высшее образование - бакалавриат"
    assert "Телефон: +7(949) 000-00-" in d["fields"]["Контакты"]
    assert d["datePosted"] == "2026-10-29"
    assert d["original_source"] == "Портал Работа России (trudvsem.ru)"


def test_parse_ru_date():
    assert mczn.parse_ru_date("Обновлено: 5 марта 2026") == "2026-03-05T00:00:00+00:00"
    assert mczn.parse_ru_date("Вакансия обновлена: 29 октября 2026") == "2026-10-29T00:00:00+00:00"
    assert mczn.parse_ru_date("без даты") is None
    assert mczn.parse_ru_date("31 февраля 2026") is None


def test_salary_parsing():
    assert mczn._salary("от 27093 руб. до 35000 руб.") == (27093, 35000)
    assert mczn._salary("от 27093 руб.") == (27093, None)
    assert mczn._salary("") == (None, None)


def _mock_site(detail_status: int = 200):
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, text=read("list.html")))
    respx.get(url__regex=r"https://m-czn\.ru/vacancy/g-[\w-]+/[\w-]+-\d+$").mock(
        return_value=httpx.Response(detail_status, text=read("detail.html"))
    )


@respx.mock
def test_fetch_with_details():
    _mock_site()
    posts = mczn.fetch(limit=3)
    assert len(posts) == 3
    assert all(isinstance(p, RawPost) for p in posts)
    p = posts[0]
    assert p.source_type == "mczn"
    assert p.source == "m-czn.ru"
    assert p.external_id == "2977183"
    assert p.published_at == "2026-10-29T00:00:00+00:00"
    assert p.text.splitlines()[0] == "Специалист по охране труда"
    assert "Зарплата: от 27093 до 35000 руб." in p.text
    assert "Обязанности: Планирование и организация обучения" in p.text
    assert "Адрес: Донецкая Народная республика, г. Макеевка" in p.text
    assert "Контакты: Телефон: +7(949)" in p.text
    assert p.extra["salary_min"] == 27093
    assert p.extra["city"] == "г. Макеевка"
    assert p.extra["fields"]["Опыт работы"] == "Без опыта"
    assert p.extra["date_only"] is True


@respx.mock
def test_request_params_and_user_agent():
    _mock_site()
    mczn.fetch(limit=3)
    req = respx.calls[0].request
    assert "RabotaDonetskBot" in req.headers["user-agent"]
    assert req.url.params["limitstart"] == "0"
    assert int(req.url.params["limit"]) >= 3


@respx.mock
def test_detail_failure_keeps_list_data(caplog):
    _mock_site(detail_status=500)
    with caplog.at_level(logging.WARNING):
        posts = mczn.fetch(limit=2)
    assert len(posts) == 2
    assert "description" not in posts[0].extra
    assert "Работодатель: ГБУСО" in posts[0].text
    assert posts[0].extra["employer"].startswith("ГБУСО")
    assert "HTTP 500" in caplog.text


@respx.mock
def test_list_failure_returns_empty(caplog):
    respx.get(LIST_URL).mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.WARNING):
        assert mczn.fetch(limit=5) == []
    assert "HTTP 503" in caplog.text


@respx.mock
def test_network_error_returns_empty():
    respx.get(LIST_URL).mock(side_effect=httpx.ConnectError("нет сети"))
    assert mczn.fetch(limit=5) == []


@respx.mock
def test_pagination_stops_on_duplicates(monkeypatch):
    monkeypatch.setattr(mczn, "FETCH_DETAILS", False)
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, text=read("list.html")))
    posts = mczn.fetch(limit=8)
    # сайт на вторую страницу вернул те же 5 карточек: новых нет, останавливаемся
    assert [p.external_id for p in posts] == ["2977183", "2977178", "2977179", "2977180", "2977181"]
    assert route.call_count == 2


@respx.mock
def test_no_details_mode(monkeypatch):
    monkeypatch.setattr(mczn, "FETCH_DETAILS", False)
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, text=read("list.html")))
    posts = mczn.fetch(limit=2)
    assert len(posts) == 2
    assert len(respx.calls) == 1
    assert "Обязанности" not in posts[0].text


def test_zero_limit():
    assert mczn.fetch(limit=0) == []
