"""Сборщик VK: разбор постов, фото, репосты, ошибки API. Без сети, ключ подставляется."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from rabota.collectors import vk
from rabota.models import SOURCE_TYPES, RawPost

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "vk"
TOKEN = "test-token-1234567890abcdef"
EMPTY = {"response": {"count": 0, "items": []}}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


WALLS = {
    "dnr.work": load("wall_dnr_work.json"),
    "club68230454": load("wall_club68230454.json"),
    "vakansiidnr": load("wall_vakansiidnr.json"),
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VK_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(vk, "_sleep", lambda seconds: None)


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def api(handlers: dict[str, object] | None = None):
    """Мок wall.get: ответ по domain, остальные паблики пустые. Значение: dict или функция."""
    handlers = handlers or {}
    calls: list[dict[str, str]] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        params = form(request)
        params["_auth"] = request.headers.get("authorization", "")
        calls.append(params)
        answer = handlers.get(params["domain"], WALLS.get(params["domain"], EMPTY))
        if callable(answer):
            return answer(request)
        return httpx.Response(200, json=answer)

    route = respx.post(vk.API_URL).mock(side_effect=side_effect)
    return route, calls


def wall(*items: dict) -> dict:
    return {"response": {"count": len(items), "items": list(items)}}


def photo(*widths: int, base: str = "https://img.example/p") -> dict:
    return {
        "type": "photo",
        "photo": {"sizes": [{"type": "x", "width": w, "height": w, "url": f"{base}{w}.jpg"} for w in widths]},
    }


def post(post_id: int = 1, **kw) -> dict:
    data = {"id": post_id, "owner_id": -100, "date": 1_700_000_000, "text": "Нужен повар", "marked_as_ads": 0}
    data.update(kw)
    return data


def one(item: dict, source: str = "dnr.work") -> list[RawPost]:
    with respx.mock:
        api({source: wall(item)})
        return [p for p in vk.fetch(limit=10) if p.source == source]


# ---------- контракт ----------


def test_contract_names():
    assert vk.NAME == "vk"
    assert "vk" in SOURCE_TYPES
    assert [s.domain for s in vk.SOURCES] == [
        "dnr.work", "club142496310", "club68230454", "rabotadnrr",
        "public136489529", "vakansiidnr", "public217224375",
    ]
    assert "dnr_doc" not in [s.domain for s in vk.SOURCES]


def test_text_post_fields():
    with respx.mock:
        api()
        posts = vk.fetch(limit=10)
    accountant = next(p for p in posts if p.external_id == "197969")
    assert isinstance(accountant, RawPost)
    assert accountant.source_type == "vk"
    assert accountant.source == "dnr.work"
    assert accountant.url == "https://vk.com/wall-38109678_197969"
    assert accountant.text.startswith("Бухгалтер- кассир.")
    assert "+79490000000" in accountant.text  # текст как есть, без обработки
    assert accountant.published_at.endswith("+00:00")
    assert accountant.image_urls == []
    assert accountant.extra["owner_id"] == -38109678


def test_published_at_is_utc_iso():
    [p] = one(post(date=1_700_000_000))
    assert p.published_at == "2023-11-14T22:13:20+00:00"


def test_pinned_skipped_in_fixtures():
    with respx.mock:
        api()
        posts = vk.fetch(limit=10)
    ids = {(p.source, p.external_id) for p in posts}
    assert ("dnr.work", "197964") not in ids  # закреплённый
    assert ("vakansiidnr", "33619") not in ids
    assert ("club68230454", "78712") not in ids
    assert ("dnr.work", "197969") in ids


def test_pinned_skipped_even_when_text_is_good():
    assert one(post(is_pinned=1)) == []


def test_ads_skipped():
    assert one(post(marked_as_ads=1)) == []


# ---------- фото ----------


def test_photo_biggest_size_by_width():
    [p] = one(post(attachments=[photo(160, 2000, 640)]))
    assert p.image_urls == ["https://img.example/p2000.jpg"]


def test_non_photo_attachments_ignored():
    [p] = one(post(attachments=[{"type": "video", "video": {"id": 1}}, photo(640)]))
    assert p.image_urls == ["https://img.example/p640.jpg"]


def test_several_photos_keep_order():
    with respx.mock:
        api()
        posts = vk.fetch(limit=10)
    carousel = next(p for p in posts if p.external_id == "34296")
    assert len(carousel.image_urls) == 5
    assert len(set(carousel.image_urls)) == 5
    assert all(u.startswith("https://") for u in carousel.image_urls)


def test_fixture_photo_is_largest():
    raw = WALLS["club68230454"]["response"]["items"][1]
    sizes = raw["attachments"][0]["photo"]["sizes"]
    biggest = max(sizes, key=lambda s: s["width"])["url"]
    with respx.mock:
        api()
        posts = vk.fetch(limit=10)
    cook = next(p for p in posts if p.external_id == "85979")
    assert cook.image_urls == [biggest]


def test_post_without_text_but_with_photo_is_kept():
    with respx.mock:
        api()
        posts = vk.fetch(limit=10)
    empty = [p for p in posts if p.source == "club68230454" and not p.text.strip()]
    assert {p.external_id for p in empty} == {"85977", "85976"}
    assert all(len(p.image_urls) == 1 for p in empty)
    assert all(p.url.startswith("https://vk.com/wall-68230454_") for p in empty)


def test_post_without_text_and_photo_dropped():
    assert one(post(text="")) == []
    assert one(post(text="  \n", attachments=[{"type": "video"}])) == []


# ---------- репосты ----------


def test_repost_with_empty_own_text_takes_origin_text_and_photo():
    origin = {"id": 7, "owner_id": -555, "text": "Требуется продавец", "attachments": [photo(640, 1280)]}
    [p] = one(post(text="", copy_history=[origin]))
    assert p.text == "Требуется продавец"
    assert p.image_urls == ["https://img.example/p1280.jpg"]
    assert p.extra["repost_of"] == "wall-555_7"
    assert p.url == "https://vk.com/wall-100_1"  # ссылка на пост паблика, не на оригинал


def test_repost_origin_ignored_when_own_text_present():
    origin = {"id": 7, "owner_id": -555, "text": "Чужой текст", "attachments": [photo(640)]}
    [p] = one(post(text="Свой текст", copy_history=[origin]))
    assert p.text == "Свой текст"
    assert p.image_urls == []
    assert "repost_of" not in p.extra


def test_repost_own_photo_and_origin_photo_deduplicated():
    own = photo(640, base="https://img.example/same")
    origin = {"id": 7, "owner_id": -555, "text": "Текст", "attachments": [photo(640, base="https://img.example/same")]}
    [p] = one(post(text="", attachments=[own], copy_history=[origin]))
    assert p.image_urls == ["https://img.example/same640.jpg"]


def test_repost_of_empty_origin_dropped():
    assert one(post(text="", copy_history=[{"id": 7, "owner_id": -555, "text": ""}])) == []


# ---------- запрос ----------


def test_request_params_and_token_not_in_url():
    with respx.mock:
        route, calls = api()
        vk.fetch(limit=10)
    assert [c["domain"] for c in calls] == [s.domain for s in vk.SOURCES]
    first = calls[0]
    assert first["v"] == "5.199"
    assert first["count"] == str(10 + vk.COUNT_RESERVE)
    assert first["_auth"] == f"Bearer {TOKEN}"
    for call in route.calls:
        assert TOKEN not in str(call.request.url)
        assert "access_token" not in str(call.request.url)


def test_limit_applies_per_public():
    items = [post(i) for i in range(1, 8)]
    with respx.mock:
        api({"dnr.work": wall(*items), "vakansiidnr": wall(*items)})
        posts = vk.fetch(limit=3)
    assert [p.external_id for p in posts if p.source == "dnr.work"] == ["1", "2", "3"]
    assert [p.external_id for p in posts if p.source == "vakansiidnr"] == ["1", "2", "3"]


def test_limit_counts_after_skipping_pinned_and_ads():
    items = [post(1, is_pinned=1), post(2, marked_as_ads=1), post(3), post(4), post(5)]
    with respx.mock:
        api({"dnr.work": wall(*items)})
        posts = [p for p in vk.fetch(limit=2) if p.source == "dnr.work"]
    assert [p.external_id for p in posts] == ["3", "4"]


def test_count_capped_at_api_maximum():
    with respx.mock:
        _, calls = api()
        vk.fetch(limit=500)
    assert calls[0]["count"] == str(vk.MAX_COUNT)


def test_pause_between_requests(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(vk, "_sleep", pauses.append)
    with respx.mock:
        api()
        vk.fetch(limit=1)
    assert pauses == [vk.REQUEST_PAUSE] * (len(vk.SOURCES) - 1)
    assert vk.REQUEST_PAUSE == 0.4


def test_mariupol_marked_with_note():
    with respx.mock:
        api({"public217224375": wall(post(5))})
        posts = vk.fetch(limit=10)
    mariupol = [p for p in posts if p.source == "public217224375"]
    assert len(mariupol) == 1
    assert "военн" in mariupol[0].extra["note"]
    assert all("note" not in p.extra for p in posts if p.source != "public217224375")


def test_passed_client_is_used_and_not_closed():
    client = httpx.Client()
    with respx.mock:
        api()
        vk.fetch(limit=1, client=client)
    assert not client.is_closed
    client.close()


# ---------- ошибки ----------


def test_missing_token_raises(monkeypatch):
    monkeypatch.setenv("VK_ACCESS_TOKEN", "")
    with respx.mock as mock:
        route = mock.post(vk.API_URL)
        with pytest.raises(vk.VkTokenMissing, match="VK_ACCESS_TOKEN"):
            vk.fetch()
        assert not route.called


def test_missing_token_variable_unset(monkeypatch):
    monkeypatch.delenv("VK_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(vk.config, "get", lambda name, default="": default)
    with pytest.raises(vk.VkError):
        vk.fetch()


def test_broken_public_does_not_break_others(caplog):
    with respx.mock:
        api({"club142496310": load("error_15.json")})
        with caplog.at_level(logging.WARNING):
            posts = vk.fetch(limit=10)
    sources = {p.source for p in posts}
    assert "club142496310" not in sources
    assert {"dnr.work", "club68230454", "vakansiidnr"} <= sources
    assert any("club142496310" in r.getMessage() and "15" in r.getMessage() for r in caplog.records)


def test_all_publics_broken_raises_with_clear_message():
    with respx.mock:
        api({s.domain: load("error_15.json") for s in vk.SOURCES})
        with pytest.raises(vk.VkError, match="не удалось получить ни один паблик") as exc:
            vk.fetch()
    assert "нет доступа" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_error_15_message():
    with respx.mock:
        api({s.domain: load("error_15.json") for s in vk.SOURCES})
        with pytest.raises(vk.VkError) as exc:
            vk.fetch()
    assert "error_code=15" in str(exc.value)
    assert "нет доступа к стене" in str(exc.value)


def test_error_29_message():
    with respx.mock:
        api({s.domain: load("error_29.json") for s in vk.SOURCES})
        with pytest.raises(vk.VkError) as exc:
            vk.fetch()
    assert "error_code=29" in str(exc.value)
    assert "лимит" in str(exc.value)


def test_error_6_retried_then_succeeds(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(vk, "_sleep", pauses.append)
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] <= 2:
            return httpx.Response(200, json=load("error_6.json"))
        return httpx.Response(200, json=wall(post(9)))

    with respx.mock:
        api({"dnr.work": flaky})
        posts = vk.fetch(limit=10)
    assert [p.external_id for p in posts if p.source == "dnr.work"] == ["9"]
    assert state["n"] == 3
    assert pauses[:2] == [1.0, 2.0]


def test_error_6_persistent_skips_public_with_message(caplog):
    with respx.mock:
        _route, calls = api({"dnr.work": load("error_6.json")})
        with caplog.at_level(logging.WARNING):
            posts = vk.fetch(limit=10)
    assert "dnr.work" not in {p.source for p in posts}
    assert sum(1 for c in calls if c["domain"] == "dnr.work") == vk.RATE_LIMIT_RETRIES + 1
    assert any("слишком часто" in r.getMessage() for r in caplog.records)


def test_network_error_retried_once_then_ok():
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectTimeout("handshake timed out")
        return httpx.Response(200, json=wall(post(3)))

    with respx.mock:
        api({"dnr.work": flaky})
        posts = vk.fetch(limit=10)
    assert [p.external_id for p in posts if p.source == "dnr.work"] == ["3"]


def test_network_error_persistent_skips_public():
    def down(request):
        raise httpx.ConnectError("no route")

    with respx.mock:
        api({"dnr.work": down})
        posts = vk.fetch(limit=10)
    assert "dnr.work" not in {p.source for p in posts}
    assert "vakansiidnr" in {p.source for p in posts}


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="<html>captcha</html>"),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={"foo": 1}),
])
def test_garbage_responses_skip_public(response):
    with respx.mock:
        api({"dnr.work": lambda request: response})
        posts = vk.fetch(limit=10)
    assert "dnr.work" not in {p.source for p in posts}
    assert "vakansiidnr" in {p.source for p in posts}


def test_token_never_leaks_into_errors_and_logs(caplog):
    leaky = {"error": {"error_code": 100, "error_msg": f"bad param {TOKEN}"}}
    with respx.mock:
        api({s.domain: leaky for s in vk.SOURCES})
        with caplog.at_level(logging.DEBUG), pytest.raises(vk.VkError) as exc:
            vk.fetch()
    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text
