"""Сборщик MAX: разбор зеркала maxoteka.ru на сохранённых страницах и fetch на подменённой сети."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from rabota.collectors import max as mx
from rabota.config import USER_AGENT
from rabota.models import RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "max"
CHANNEL = "vakansii_donetsk_dnr"
FULL_MID = "mid.ffffc117036e556e01a183ad49ff216f"   # второй пост образца, у него полный текст в post_page


def load(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(mx.time, "sleep", calls.append)
    return calls


def mock_mirror(router: respx.MockRouter, post_status: int = 200):
    """Зеркало: у каждого канала один и тот же список, у каждого поста одна страница."""
    for ch in mx.SOURCES:
        router.get(f"https://maxoteka.ru/channel/{ch}").respond(
            200, text=load("channel_listing").replace(CHANNEL, ch)
        )
    router.get(url__regex=r"https://maxoteka\.ru/channel/[^/]+/post/mid\.[0-9a-f]+").respond(
        post_status, text=load("post_page")
    )


def test_constants():
    assert mx.NAME == "max"
    assert mx.SOURCES == ["channel_rabotadnr", "vakansii_donetsk_dnr", "vakansii_enakievo_dnr", "vakansii_gorlovka"]


# ---------- разбор ----------

def test_parse_listing():
    items = mx.parse_listing(load("channel_listing"), CHANNEL)
    assert len(items) == 5
    first = items[0]
    assert first["max_key"] == "AaGD34TOWvE"
    assert first["mid"] == "mid.ffffc117036e556e01a183df84ce5af1"
    assert first["mirror_url"] == f"https://maxoteka.ru/channel/{CHANNEL}/post/{first['mid']}"
    assert first["date"] == "2026-10-28T18:57:00+00:00"
    assert first["views"] == 94
    assert first["images"] and first["images"][0].startswith("https://i.oneme.ru/")
    assert first["text"].startswith("В сеть супермаркетов #ТРАМВАЙ @user требуются")
    assert "<svg" not in first["text"] and "ei-zap" not in first["text"]


def test_parse_listing_newest_first_and_truncated_text():
    items = mx.parse_listing(load("channel_listing"), CHANNEL)
    dates = [i["date"] for i in items]
    assert dates == sorted(dates, reverse=True)
    assert items[1]["text"].endswith("…")                 # в списке текст обрезан
    assert items[2]["images"] == []                       # пост без картинки


def test_parse_listing_ignores_other_channel_links():
    items = mx.parse_listing(load("channel_listing"), "vakansii_gorlovka")
    assert all(i["max_key"] is None for i in items)       # ссылки max.ru чужого канала не берём
    assert all(i["mid"] for i in items)


def test_parse_listing_empty_page():
    assert mx.parse_listing(load("channel_empty"), "channel_rabotadnr") == []


def test_parse_post_page():
    data = mx.parse_post_page(load("post_page"))
    assert data is not None
    assert data["date"] == "2026-10-28T18:02:14+00:00"
    assert data["text"].startswith("Набор мужчин в элитные подразделения")
    assert "Лична…" not in data["text"] and data["text"].endswith("https://clck.ru/3Vkeva")
    assert data["image"].startswith("https://i.oneme.ru/")


def test_parse_post_page_without_json_ld():
    assert mx.parse_post_page("<html><body>нет данных</body></html>") is None
    broken = '<html><script type="application/ld+json">{не json</script></html>'
    assert mx.parse_post_page(broken) is None


def test_list_date_bad_values():
    assert mx._list_date("32.13.2026 99:99") is None
    assert mx._list_date("вчера") is None


# ---------- fetch ----------

@respx.mock
def test_fetch_full_text_from_post_page():
    mock_mirror(respx.mock)
    with httpx.Client() as client:
        posts = mx.fetch(limit=10, client=client)
    # channel_rabotadnr в подмене тоже отдаёт список, поэтому постов 5 на каждый канал
    assert len(posts) == 5 * len(mx.SOURCES)
    post = next(p for p in posts if p.source == CHANNEL and p.external_id == "AaGDrUn_IW8")
    assert isinstance(post, RawPost)
    assert post.source_type == "max"
    assert post.url == f"https://max.ru/{CHANNEL}/AaGDrUn_IW8"
    assert post.text.endswith("https://clck.ru/3Vkeva")   # полный текст со страницы поста
    assert post.published_at == "2026-10-28T18:02:14+00:00"
    assert post.extra["mid"] == FULL_MID and post.extra["views"] == 108
    assert "text_may_be_truncated" not in post.extra
    json.dumps(post.to_dict(), ensure_ascii=False)


@respx.mock
def test_fetch_limit_per_channel():
    mock_mirror(respx.mock)
    with httpx.Client() as client:
        posts = mx.fetch(limit=2, client=client)
    assert len(posts) == 2 * len(mx.SOURCES)
    assert [p.external_id for p in posts[:2]] == ["AaGD34TOWvE", "AaGDrUn_IW8"]


@respx.mock
def test_fetch_falls_back_to_listing_when_post_page_fails():
    mock_mirror(respx.mock, post_status=500)
    with httpx.Client() as client:
        posts = mx.fetch(limit=2, client=client)
    assert len(posts) == 2 * len(mx.SOURCES)
    p = posts[1]
    assert p.text.endswith("…") and p.extra["text_may_be_truncated"] is True
    assert p.published_at == "2026-10-28T18:02:00+00:00"


@respx.mock
def test_fetch_skips_broken_and_empty_channels(caplog):
    mock_mirror(respx.mock)
    respx.get("https://maxoteka.ru/channel/channel_rabotadnr").respond(200, text=load("channel_empty"))
    respx.get("https://maxoteka.ru/channel/vakansii_gorlovka").respond(503)
    respx.get("https://maxoteka.ru/channel/vakansii_enakievo_dnr").mock(side_effect=httpx.ReadTimeout("таймаут"))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        posts = mx.fetch(limit=3, client=client)
    assert {p.source for p in posts} == {CHANNEL}
    assert len(posts) == 3
    assert "channel_rabotadnr" in caplog.text and "vakansii_gorlovka" in caplog.text


@respx.mock
def test_fetch_pauses_and_user_agent(no_sleep):
    mock_mirror(respx.mock)
    mx.fetch(limit=1)                                    # свой клиент
    assert len(respx.calls) == 2 * len(mx.SOURCES)       # список и одна страница поста на канал
    assert len(no_sleep) == len(respx.calls) - 1
    assert all(0.5 <= s <= 1.0 for s in no_sleep)
    assert all(c.request.headers["user-agent"] == USER_AGENT for c in respx.calls)


@respx.mock
def test_fetch_does_not_use_robots_blocked_endpoint():
    mock_mirror(respx.mock)
    with httpx.Client() as client:
        mx.fetch(limit=10, client=client)
    assert all("posts-more.php" not in str(c.request.url) for c in respx.calls)
