"""Тесты сборщика hh.ru: встроенный JSON выдачи, блокировки и анти-бот. Без сети."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import hh
from rabota.models import SOURCE_TYPES, RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "hh"
SEARCH = "https://hh.ru/search/vacancy"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(hh.time, "sleep", lambda _s: None)


def test_contract():
    assert hh.NAME == "hh"
    assert hh.SOURCES == ("hh",)
    assert "hh" in SOURCE_TYPES
    assert hh.AREAS["Донецк"] == 118 and hh.AREAS["ДНР"] == 2134


def test_parse_search():
    vacancies, total = hh.parse_search(read("search.html"))
    assert len(vacancies) == 8
    assert total == 759
    assert vacancies[0]["vacancyId"]


def test_captcha_page_is_blocked_error():
    with pytest.raises(hh.BlockedError):
        hh.parse_search(read("captcha.html"))


def test_page_without_state_is_blocked_error():
    with pytest.raises(hh.BlockedError, match="нет встроенного списка"):
        hh.parse_search("<html><body>другая вёрстка</body></html>")


@respx.mock
def test_fetch_returns_raw_posts_sorted():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=read("search.html")))
    posts = hh.fetch(limit=5)
    assert len(posts) == 5
    assert all(isinstance(p, RawPost) for p in posts)
    dates = [p.published_at for p in posts]
    assert dates == sorted(dates, reverse=True)
    p = posts[0]
    assert p.source_type == "hh"
    assert p.source == "hh.ru"
    assert p.url == f"https://hh.ru/vacancy/{p.external_id}"
    assert p.published_at.endswith("+00:00")
    assert p.extra["vacancyId"] == int(p.external_id)
    assert p.extra["area"]["name"]
    # служебные поля выдачи в extra не попадают
    assert "vacancyProperties" not in p.extra


@respx.mock
def test_request_params_and_user_agent():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=read("search.html")))
    hh.fetch(limit=3)
    req = respx.calls[0].request
    assert "RabotaDonetskBot" in req.headers["user-agent"]
    assert req.url.params["area"] == "2134"
    assert req.url.params["order_by"] == "publication_time"
    assert req.url.params["enable_snippets"] == "true"


@respx.mock
def test_text_has_snippets_and_fields():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=read("search.html")))
    posts = hh.fetch(limit=8)
    first = posts[0]
    assert first.text.splitlines()[0] == first.extra["name"]
    assert "Работодатель:" in first.text
    assert "Город:" in first.text
    assert any("Требования:" in p.text for p in posts)
    assert any("Условия:" in p.text for p in posts)
    assert any("Зарплата: от" in p.text for p in posts)


def test_text_formats_salary_and_schedule():
    v = {
        "vacancyId": 1, "name": "Бариста", "company": {"name": "Кофейня"},
        "compensation": {"from": 40000, "to": 60000, "currencyCode": "RUR", "gross": False, "mode": "MONTH"},
        "area": {"name": "Донецк"}, "workExperience": "noExperience", "employmentForm": "PART",
        "workScheduleByDays": [{"workScheduleByDaysElement": ["TWO_ON_TWO_OFF"]}],
        "workingHours": [{"workingHoursElement": ["HOURS_12"]}],
        "workFormats": [{"workFormatsElement": ["ON_SITE"]}],
        "address": {"displayName": "Донецк, Артёма, 1", "districtDto": {"name": "Ворошиловский"}},
        "snippet": {"req": "Опыт   не нужен.", "resp": None},
    }
    text = hh.build_text(v)
    assert "Зарплата: от 40000 до 60000 руб. в месяц, на руки" in text
    assert "Опыт работы: не нужен" in text
    assert "Занятость: частичная занятость" in text
    assert "График: 2/2" in text
    assert "Часов в смене: 12" in text
    assert "Формат работы: на месте работодателя" in text
    assert "Район: Ворошиловский" in text
    assert "Требования: Опыт не нужен." in text
    assert "Обязанности" not in text


def test_no_salary_in_text():
    v = {"vacancyId": 2, "name": "Курьер", "compensation": {"noCompensation": {}}}
    assert "Зарплата" not in hh.build_text(v)


@respx.mock
@pytest.mark.parametrize("status", [403, 429, 503])
def test_blocked_status_returns_empty_with_warning(status, caplog):
    respx.get(SEARCH).mock(return_value=httpx.Response(status, text="blocked"))
    with caplog.at_level(logging.WARNING):
        assert hh.fetch(limit=5) == []
    assert "анти-бот" in caplog.text


@respx.mock
def test_captcha_returns_empty_with_warning(caplog):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=read("captcha.html")))
    with caplog.at_level(logging.WARNING):
        assert hh.fetch(limit=5) == []
    assert "капчу не обходим" in caplog.text


@respx.mock
def test_network_error_returns_empty(caplog):
    respx.get(SEARCH).mock(side_effect=httpx.ReadTimeout("таймаут"))
    with caplog.at_level(logging.WARNING):
        assert hh.fetch(limit=5) == []
    assert "ошибка сети" in caplog.text


@respx.mock
def test_one_area_blocked_others_survive():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["area"] == "2140":
            return httpx.Response(403)
        return httpx.Response(200, text=read("search.html"))

    respx.get(SEARCH).mock(side_effect=handler)
    posts = hh.fetch(limit=3, areas=(2140, 118))
    assert len(posts) == 3


@respx.mock
def test_areas_deduplicated():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=read("search.html")))
    posts = hh.fetch(limit=8, areas=(118, 2134))
    assert len(posts) == 8
    assert len({p.external_id for p in posts}) == 8


def test_zero_limit():
    assert hh.fetch(limit=0) == []


# ------------------------------------------------------------ полное описание со страницы вакансии

def _search_item(vid: int = 134290618) -> dict:
    return {
        "vacancyId": vid, "name": "Оператор call-центра", "company": {"visibleName": "ZebraCall"},
        "area": {"name": "Донецк"}, "workExperience": "noExperience", "publicationTime": {"@timestamp": 1793600000},
        "snippet": {"req": "У тебя есть ноутбук/компьютер и наушники с микрофоном. Есть wifi или проводной интернет."},
    }


def test_description_to_text_keeps_heading_with_its_list():
    text = hh.description_to_text("<p><strong>Чего мы ждем от тебя?</strong></p><ul><li>Один</li><li>Два</li></ul><p>Конец</p>")
    assert text == "Чего мы ждем от тебя?\n- Один\n- Два\n\nКонец"


def test_fetch_description_reads_full_text_from_vacancy_page():
    with respx.mock:
        respx.get("https://hh.ru/vacancy/134290618").mock(return_value=httpx.Response(200, text=read("vacancy.html")))
        text = hh.fetch_description(httpx.Client(), "134290618")
    assert "Тихое место" in text and "Грамотная речь" in text


def test_fetch_description_survives_block():
    with respx.mock:
        respx.get("https://hh.ru/vacancy/1").mock(return_value=httpx.Response(403))
        assert hh.fetch_description(httpx.Client(), "1") is None


def test_full_description_gives_all_requirements():
    from rabota.pipeline.run import process

    with respx.mock:
        respx.get("https://hh.ru/vacancy/134290618").mock(return_value=httpx.Response(200, text=read("vacancy.html")))
        description = hh.fetch_description(httpx.Client(), "134290618")
    vacancies = process(hh.to_raw_post(_search_item(), description))
    assert len(vacancies) == 1
    req = vacancies[0].requirements
    for part in ("ноутбук", "wifi", "Тихое место", "Грамотная речь"):
        assert part in req, req


@respx.mock
def test_fetch_requests_page_only_for_unknown_vacancies(monkeypatch):
    page = read("search.html")
    vacancies, _ = hh.parse_search(page)
    first, second = (str(v["vacancyId"]) for v in vacancies[:2])
    respx.get(SEARCH).mock(return_value=httpx.Response(200, text=page))
    detail = respx.get(url__regex=r"https://hh\.ru/vacancy/\d+").mock(return_value=httpx.Response(200, text=read("vacancy.html")))
    posts = hh.fetch(limit=2, detail=True, known={first})
    assert detail.call_count == 1
    by_id = {p.external_id: p for p in posts}
    assert "Тихое место" in by_id[second].text
    assert "Тихое место" not in by_id[first].text
