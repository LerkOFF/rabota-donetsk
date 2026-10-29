"""Сборщик Telegram: разбор сохранённых страниц превью и fetch на подменённой сети."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import telegram as tg
from rabota.config import USER_AGENT
from rabota.models import RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "telegram"


def load(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def message(channel: str, post_id: int, text: str | None = "Вакансия", photo: str | None = None) -> str:
    """Минимальная разметка одного сообщения превью."""
    body = f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>' if text is not None else ""
    img = (
        f'<a class="tgme_widget_message_photo_wrap" href="https://t.me/{channel}/{post_id}" '
        f"style=\"width:800px;background-image:url('{photo}')\"></a>"
        if photo else ""
    )
    return (
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{channel}/{post_id}">'
        f'{img}{body}<div class="tgme_widget_message_info"><span class="tgme_widget_message_meta">'
        f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{post_id}">'
        f'<time datetime="2026-10-26T12:{post_id % 60:02d}:00+00:00">12:00</time></a></span></div></div></div>'
    )


def page(channel: str, ids: list[int]) -> str:
    return "<html><body>" + "".join(message(channel, i) for i in ids) + "</body></html>"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Паузы не ждём, но запоминаем."""
    calls: list[float] = []
    monkeypatch.setattr(tg.time, "sleep", calls.append)
    return calls


# ---------- разбор страницы ----------

def test_constants():
    assert tg.NAME == "telegram"
    assert "rabotadnr180" not in tg.SOURCES
    assert len(tg.SOURCES) == 8 and "Vakansiii_dnr" in tg.SOURCES


def test_parse_page_basic():
    posts = tg.parse_page(load("dnr_rabotaz"), "dnr_rabotaz")
    assert len(posts) == 20
    ids = [int(p.external_id) for p in posts]
    assert ids == sorted(ids), "порядок страницы: от старых к новым"
    first = posts[0]
    assert isinstance(first, RawPost)
    assert first.source_type == "telegram" and first.source == "dnr_rabotaz"
    assert first.external_id == "13888"
    assert first.url == "https://t.me/dnr_rabotaz/13888"
    assert first.published_at == "2026-10-26T12:53:28+00:00"


def test_text_br_to_newlines_no_html():
    first = tg.parse_page(load("dnr_rabotaz"), "dnr_rabotaz")[0]
    assert first.text.startswith("Официант\nЗарплата по договоренности.\n\nОбязанности:\n• Встреча")
    assert "Контакты:\n+7949" in first.text
    assert "<" not in first.text and "&amp;" not in first.text


def test_photo_only_post_is_kept():
    posts = tg.parse_page(load("rabota_donetskv"), "rabota_donetskv")
    only_photo = [p for p in posts if p.text == ""]
    assert only_photo, "в образце есть пост без текста"
    post = only_photo[0]
    assert post.external_id == "20318"
    assert len(post.image_urls) == 1 and post.image_urls[0].startswith("https://cdn")
    assert "'" not in post.image_urls[0] and "url(" not in post.image_urls[0]


def test_text_post_with_photo():
    posts = tg.parse_page(load("rabota_donetskv"), "rabota_donetskv")
    with_both = [p for p in posts if p.text and p.image_urls]
    assert with_both and all(u.startswith("https://") for p in with_both for u in p.image_urls)


def test_post_without_photo_has_empty_image_list():
    posts = tg.parse_page(load("dnr_rabotaz"), "dnr_rabotaz")
    assert any(p.image_urls == [] for p in posts)


def test_views_links_video_in_extra():
    posts = {p.external_id: p for p in tg.parse_page(load("dnr_rabotaz"), "dnr_rabotaz")}
    assert posts["13888"].extra["views"] == 5620          # "5.62K"
    video = posts["13890"]
    assert video.extra["has_video"] is True
    assert video.extra["links"] == ["https://trk.ppdu.ru/click/2nAKIaKZ?erid=CQH36pWzJqVGXC5oLP8WVVNCNqJmbhiUPijGiu4zpwPd7G&sub1=eda1"]


def test_all_posts_serialize_to_json():
    posts = tg.parse_page(load("rabota_donetskv"), "rabota_donetskv")
    json.dumps([p.to_dict() for p in posts], ensure_ascii=False)


def test_foreign_channel_posts_are_skipped():
    assert tg.parse_page(load("dnr_rabotaz"), "another_channel") == []


def test_channel_name_case_is_insensitive():
    html = page("vakansiii_dnr", [5, 6])
    posts = tg.parse_page(html, "Vakansiii_dnr")
    assert [p.url for p in posts] == ["https://t.me/Vakansiii_dnr/5", "https://t.me/Vakansiii_dnr/6"]


def test_closed_channel_page_has_no_posts():
    html = '<html><body><div class="tgme_page"><div class="tgme_page_title">Чат</div></div></body></html>'
    assert tg.parse_page(html, "rabotadnr180") == []


@pytest.mark.parametrize("raw, expected", [("5.62K", 5620), ("143", 143), ("1,2M", 1_200_000), ("", None), ("abc", None)])
def test_parse_views(raw, expected):
    assert tg._parse_views(raw) == expected


def test_naive_datetime_is_treated_as_utc():
    assert tg._iso_utc("2026-10-26T12:00:00") == "2026-10-26T12:00:00+00:00"
    assert tg._iso_utc("2026-10-26T15:00:00+03:00") == "2026-10-26T12:00:00+00:00"
    assert tg._iso_utc("мусор") is None and tg._iso_utc(None) is None


# ---------- fetch ----------

def mock_all_sources(router: respx.MockRouter, html_by_channel: dict[str, str] | None = None):
    html_by_channel = html_by_channel or {}
    for ch in tg.SOURCES:
        router.get(f"https://t.me/s/{ch}").respond(200, text=html_by_channel.get(ch, page(ch.lower(), list(range(100, 120)))))


@respx.mock
def test_fetch_limit_per_channel_newest_first():
    mock_all_sources(respx.mock)
    with httpx.Client() as client:
        posts = tg.fetch(limit=3, client=client)
    assert len(posts) == 3 * len(tg.SOURCES)
    by_source: dict[str, list[int]] = {}
    for p in posts:
        by_source.setdefault(p.source, []).append(int(p.external_id))
    assert set(by_source) == set(tg.SOURCES)
    assert all(ids == [119, 118, 117] for ids in by_source.values())


@respx.mock
def test_fetch_on_fixtures():
    mock_all_sources(respx.mock, {"dnr_rabotaz": load("dnr_rabotaz"), "rabota_donetskv": load("rabota_donetskv")})
    with httpx.Client() as client:
        posts = tg.fetch(limit=10, client=client)
    rz = [p for p in posts if p.source == "dnr_rabotaz"]
    assert len(rz) == 10 and rz[0].external_id == "13907"


@respx.mock
def test_fetch_skips_broken_channels(caplog):
    mock_all_sources(respx.mock)
    respx.get("https://t.me/s/dnr_rabotaz").respond(500)
    respx.get("https://t.me/s/dnr_rabotaq").mock(side_effect=httpx.ConnectTimeout("таймаут"))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        posts = tg.fetch(limit=2, client=client)
    sources = {p.source for p in posts}
    assert "dnr_rabotaz" not in sources and "dnr_rabotaq" not in sources
    assert len(sources) == len(tg.SOURCES) - 2
    assert "dnr_rabotaz" in caplog.text and "dnr_rabotaq" in caplog.text


@respx.mock
def test_fetch_closed_channel_gives_nothing():
    mock_all_sources(respx.mock)
    respx.get("https://t.me/s/chadachkadnr").respond(200, text="<html><body>закрыто</body></html>")
    with httpx.Client() as client:
        posts = tg.fetch(limit=2, client=client)
    assert all(p.source != "chadachkadnr" for p in posts)
    assert len(posts) == 2 * (len(tg.SOURCES) - 1)


@respx.mock
def test_fetch_paginates_with_before(no_sleep, monkeypatch):
    monkeypatch.setattr(tg, "SOURCES", ["only"])
    route = respx.get("https://t.me/s/only")
    route.side_effect = lambda request: (
        httpx.Response(200, text=page("only", list(range(71, 91))))
        if request.url.params.get("before") is None
        else httpx.Response(200, text=page("only", list(range(51, 71))))
    )
    with httpx.Client() as client:
        posts = tg.fetch(limit=25, client=client)
    assert [int(p.external_id) for p in posts] == list(range(90, 65, -1))
    assert route.call_count == 2
    assert route.calls[1].request.url.params["before"] == "71"
    assert len(no_sleep) == 1 and 0.5 <= no_sleep[0] <= 1.0


@respx.mock
def test_fetch_pause_between_requests(no_sleep):
    mock_all_sources(respx.mock)
    with httpx.Client() as client:
        tg.fetch(limit=5, client=client)
    assert len(no_sleep) == len(tg.SOURCES) - 1          # по одному запросу на канал, перед первым без паузы
    assert all(0.5 <= s <= 1.0 for s in no_sleep)


@respx.mock
def test_fetch_own_client_uses_user_agent():
    mock_all_sources(respx.mock)
    tg.fetch(limit=1)
    assert all(c.request.headers["user-agent"] == USER_AGENT for c in respx.calls)


@respx.mock
def test_fetch_stops_on_repeating_page(monkeypatch):
    """Если сервер на ?before вернул ту же страницу, не зацикливаемся."""
    monkeypatch.setattr(tg, "SOURCES", ["only"])
    route = respx.get("https://t.me/s/only").respond(200, text=page("only", [10, 11, 12]))
    with httpx.Client() as client:
        posts = tg.fetch(limit=50, client=client)
    assert len(posts) == 3
    assert route.call_count <= 2
