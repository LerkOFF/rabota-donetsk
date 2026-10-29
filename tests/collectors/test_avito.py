"""Тесты сборщика Авито: разбор сохранённых страниц и поведение при блокировке. Без сети."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import avito
from rabota.models import SOURCE_TYPES, RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "avito"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)  # 15:00 по Донецку


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(avito.time, "sleep", lambda _s: None)


def test_contract():
    assert avito.NAME == "avito"
    assert "avito" in SOURCE_TYPES
    assert len(avito.SOURCES) == 5
    assert all(u.startswith("https://www.avito.ru/") for u in avito.SOURCES)
    assert any("makeevka" in u for u in avito.SOURCES)
    assert any("podrabotka" in u for u in avito.SOURCES)
    assert any("kazhdyy_den" in u for u in avito.SOURCES)


def test_parse_html_cards():
    posts = avito.parse_listing(read("listing_html.html"), "sec", now=NOW)
    assert [p.external_id for p in posts] == ["4100000001", "4100000002", "4100000003", "4100000004"]
    first = posts[0]
    assert isinstance(first, RawPost)
    assert first.source_type == "avito" and first.source == "avito"
    assert first.url == "https://www.avito.ru/donetsk/vakansii/barista_v_kofeynyu_4100000001"
    assert "Бариста в кофейню" in first.text and "2/2" in first.text
    assert first.extra["price"] == "45 000 ₽ в месяц"
    assert first.extra["price_value"] == 45000
    assert first.extra["district"] == "Ворошиловский р-н"
    assert first.extra["description"].startswith("Нужен бариста")
    assert first.extra["section"] == "sec"
    assert first.image_urls == ["https://00.img.avito.st/image/1/barista.jpg"]
    assert first.published_at == "2026-10-29T09:00:00Z"  # 3 часа назад


def test_html_dates_and_missing_fields():
    posts = {p.external_id: p for p in avito.parse_listing(read("listing_html.html"), now=NOW)}
    assert posts["4100000002"].published_at == "2026-10-28T15:20:00Z"  # вчера 18:20 по Донецку
    assert posts["4100000003"].published_at == "2026-10-14T21:00:00Z"  # 15 октября, полночь по Донецку
    assert posts["4100000004"].published_at == "2026-10-29T06:15:00Z"  # сегодня 09:15
    assert posts["4100000003"].extra["price_value"] is None
    assert posts["4100000003"].image_urls == []
    # srcset: берётся последняя (самая крупная) картинка, адрес без протокола дополняется
    assert posts["4100000002"].image_urls == ["https://00.img.avito.st/image/1/kurer_big.jpg"]


def test_parse_initial_data_json():
    posts = avito.parse_listing(read("listing_json.html"), "mk")
    assert [p.external_id for p in posts] == ["4200000001", "4200000002", "4200000003"]
    povar = posts[0]
    assert povar.url == "https://www.avito.ru/makeevka/vakansii/povar-universal_4200000001"
    assert povar.extra["price_value"] == 38000
    assert povar.extra["price"] == "38 000 ₽ в месяц"
    assert povar.extra["district"] == "Червоногвардейский р-н"
    assert povar.extra["city"] == "Макеевка"
    assert povar.extra["category"] == "Вакансии"
    assert povar.image_urls == ["https://50.img.avito.st/image/1/povar_636.jpg"]
    assert povar.published_at == "2025-10-29T06:40:00Z"
    assert "график 5/2" in povar.text
    gruzchik = posts[1]
    assert gruzchik.extra["price_value"] is None
    assert gruzchik.image_urls == ["https://50.img.avito.st/image/1/gruzchik.jpg"]
    assert posts[2].image_urls == []


def test_to_dict_is_json_ready():
    import json

    posts = avito.parse_listing(read("listing_json.html"))
    json.dumps([p.to_dict() for p in posts], ensure_ascii=False)


def test_blocked_page_detection():
    assert avito.is_blocked(429, "")
    assert avito.is_blocked(200, read("blocked.html"))
    assert not avito.is_blocked(200, read("listing_html.html"))
    assert avito.parse_listing(read("blocked.html")) == []


def test_parse_date_text():
    assert avito.parse_date_text("25 минут назад", NOW) == "2026-10-29T11:35:00Z"
    assert avito.parse_date_text("2 дня назад", NOW) == "2026-10-27T12:00:00Z"
    assert avito.parse_date_text("неделю назад", NOW) == "2026-10-22T12:00:00Z"
    assert avito.parse_date_text("20 декабря", NOW) == "2025-12-19T21:00:00Z"  # будущая дата значит прошлый год
    assert avito.parse_date_text("", NOW) is None
    assert avito.parse_date_text("что-то странное", NOW) is None


@respx.mock
def test_fetch_collects_limit_per_section_and_dedupes():
    pages = {
        "https://www.avito.ru/donetsk/vakansii": read("listing_html.html"),
        "https://www.avito.ru/donetck/vakansii": read("listing_html.html"),  # те же объявления
        "https://www.avito.ru/makeevka/vakansii": read("listing_json.html"),
    }
    for url in avito.SOURCES:
        body = pages.get(url, read("listing_json.html"))
        respx.get(url).mock(return_value=httpx.Response(200, text=body))
    with httpx.Client() as client:
        posts = avito.fetch(limit=2, client=client)
    ids = [p.external_id for p in posts]
    assert ids[:2] == ["4100000001", "4100000002"]
    assert len(ids) == len(set(ids))  # дублей между разделами нет
    # donetck отдал те же два (отсеяны), макеевка отдала два из json
    assert "4200000001" in ids and "4200000002" in ids
    assert "4200000003" not in ids  # третье объявление за пределом limit


@respx.mock
def test_fetch_uses_date_sort_param():
    route = respx.get(avito.SOURCES[0]).mock(return_value=httpx.Response(200, text=read("listing_html.html")))
    for url in avito.SOURCES[1:]:
        respx.get(url).mock(return_value=httpx.Response(200, text=""))
    with httpx.Client() as client:
        avito.fetch(limit=1, client=client)
    assert route.calls[0].request.url.params["s"] == "104"


@respx.mock
def test_fetch_blocked_returns_empty_and_warns(caplog):
    routes = [respx.get(u).mock(return_value=httpx.Response(429, text=read("blocked.html"))) for u in avito.SOURCES]
    with caplog.at_level(logging.WARNING, logger="rabota.collectors.avito"), httpx.Client() as client:
        posts = avito.fetch(limit=10, client=client)
    assert posts == []
    assert "Авито закрыл доступ" in caplog.text
    # после первого отказа остальные разделы не дёргаем
    assert routes[0].call_count == 1
    assert sum(r.call_count for r in routes[1:]) == 0


@respx.mock
def test_fetch_captcha_page_with_200_is_blocked(caplog):
    respx.get(avito.SOURCES[0]).mock(return_value=httpx.Response(200, text=read("blocked.html")))
    with caplog.at_level(logging.WARNING, logger="rabota.collectors.avito"), httpx.Client() as client:
        assert avito.fetch(client=client) == []
    assert "Авито закрыл доступ" in caplog.text


@respx.mock
def test_fetch_block_midway_keeps_collected():
    respx.get(avito.SOURCES[0]).mock(return_value=httpx.Response(200, text=read("listing_html.html")))
    respx.get(avito.SOURCES[1]).mock(return_value=httpx.Response(429, text="slow down"))
    with httpx.Client() as client:
        posts = avito.fetch(limit=3, client=client)
    assert [p.external_id for p in posts] == ["4100000001", "4100000002", "4100000003"]


@respx.mock
def test_fetch_network_error_skips_section():
    respx.get(avito.SOURCES[0]).mock(side_effect=httpx.ConnectError("нет сети"))
    respx.get(avito.SOURCES[1]).mock(return_value=httpx.Response(200, text=read("listing_json.html")))
    for url in avito.SOURCES[2:]:
        respx.get(url).mock(return_value=httpx.Response(200, text=""))
    with httpx.Client() as client:
        posts = avito.fetch(limit=1, client=client)
    assert [p.external_id for p in posts] == ["4200000001"]
