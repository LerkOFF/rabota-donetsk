"""Тесты сборщика Работы России: разбор сохранённого ответа API и сбои источника. Без сети."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import trudvsem
from rabota.models import SOURCE_TYPES, RawPost

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "trudvsem" / "region_dnr.json"
API = "https://opendata.trudvsem.ru/api/v1/vacancies/region/9300000000000"


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(trudvsem.time, "sleep", lambda _s: None)


def test_contract():
    assert trudvsem.NAME == "trudvsem"
    assert trudvsem.SOURCES == ("trudvsem",)
    assert "trudvsem" in SOURCE_TYPES


@respx.mock
def test_fetch_returns_raw_posts():
    route = respx.get(API).mock(return_value=httpx.Response(200, json=load()))
    posts = trudvsem.fetch(limit=4)
    assert route.called
    assert len(posts) == 4
    assert all(isinstance(p, RawPost) for p in posts)
    p = posts[0]
    assert p.source_type == "trudvsem"
    assert p.source == "trudvsem"
    assert p.external_id == "a30aacf8-d2b7-11f1-80bc-ff9d6c68c0a4"
    assert p.url.startswith("https://trudvsem.ru/vacancy/card/")
    # date_modify 13:32:48+03:00 в UTC
    assert p.published_at == "2026-10-28T10:32:48+00:00"
    assert p.image_urls == []


@respx.mock
def test_request_uses_user_agent_and_limit():
    route = respx.get(API).mock(return_value=httpx.Response(200, json=load()))
    trudvsem.fetch(limit=2)
    req = route.calls.last.request
    assert "RabotaDonetskBot" in req.headers["user-agent"]
    assert req.url.params["offset"] == "0"
    assert int(req.url.params["limit"]) >= 2


@respx.mock
def test_text_is_readable_and_has_main_fields():
    respx.get(API).mock(return_value=httpx.Response(200, json=load()))
    p = trudvsem.fetch(limit=1)[0]
    lines = p.text.splitlines()
    assert lines[0] == "Уборщик производственных и служебных помещений"
    assert "Работодатель: ГБДОУ" in p.text
    assert "Зарплата: от 27093 руб." in p.text
    assert "Обязанности: дезинфицирование" in p.text
    assert "Требования: коммуникабельность" in p.text
    assert "График: Полный рабочий день" in p.text
    assert "Адрес: Донецкая Народная республика, Шахтерский район" in p.text
    assert "Телефон: +7(949)" in p.text


@respx.mock
def test_extra_keeps_all_structured_fields():
    data = load()
    respx.get(API).mock(return_value=httpx.Response(200, json=data))
    p = trudvsem.fetch(limit=1)[0]
    original = data["results"]["vacancies"][0]["vacancy"]
    assert p.extra == original
    assert p.extra["salary_min"] == 27093
    assert p.extra["schedule"] == "Полный рабочий день"
    assert p.extra["company"]["name"].startswith("ГБДОУ")


@respx.mock
def test_sorted_by_freshness_even_if_api_order_changes():
    data = load()
    data["results"]["vacancies"].reverse()
    respx.get(API).mock(return_value=httpx.Response(200, json=data))
    posts = trudvsem.fetch(limit=3)
    dates = [p.published_at for p in posts]
    assert dates == sorted(dates, reverse=True)
    assert posts[0].external_id == "a30aacf8-d2b7-11f1-80bc-ff9d6c68c0a4"


def test_salary_text_variants():
    assert trudvsem._salary_text({"salary_min": 28000, "salary_max": 35000}) == "от 28000 до 35000 руб."
    assert trudvsem._salary_text({"salary_min": 27093, "salary_max": 27093}) == "от 27093 руб."
    assert trudvsem._salary_text({"salary_min": 0, "salary_max": 0}) == ""


def test_build_text_skips_empty_fields():
    v = {"id": "x", "job-name": "Повар", "company": {}, "duty": "", "salary_min": 0}
    assert trudvsem.build_text(v) == "Повар"


@respx.mock
def test_published_at_falls_back_to_creation_date():
    data = load()
    v = copy.deepcopy(data["results"]["vacancies"][0])
    del v["vacancy"]["date_modify"]
    data["results"]["vacancies"] = [v]
    respx.get(API).mock(return_value=httpx.Response(200, json=data))
    assert trudvsem.fetch(limit=1)[0].published_at == "2026-10-28T00:00:00+00:00"


@respx.mock
def test_http_error_returns_empty_and_warns(caplog):
    respx.get(API).mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.WARNING):
        assert trudvsem.fetch(limit=3) == []
    assert "HTTP 503" in caplog.text


@respx.mock
def test_api_error_body_returns_empty():
    body = {"status": "500", "meta": {"error": "ошибка"}}
    respx.get(API).mock(return_value=httpx.Response(200, json=body))
    assert trudvsem.fetch(limit=3) == []


@respx.mock
def test_network_error_returns_empty():
    respx.get(API).mock(side_effect=httpx.ConnectTimeout("timeout"))
    assert trudvsem.fetch(limit=3) == []


@respx.mock
def test_retry_then_success():
    route = respx.get(API).mock(side_effect=[httpx.Response(502), httpx.Response(200, json=load())])
    assert len(trudvsem.fetch(limit=2)) == 2
    assert route.call_count == 2


@respx.mock
def test_uses_given_client_and_does_not_close_it():
    respx.get(API).mock(return_value=httpx.Response(200, json=load()))
    with httpx.Client() as client:
        assert len(trudvsem.fetch(limit=2, client=client)) == 2
        assert not client.is_closed


def test_zero_limit():
    assert trudvsem.fetch(limit=0) == []
