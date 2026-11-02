"""Подсказки поиска: чистая функция отбора, эндпоинт /suggest, кэш и вес страницы."""
from __future__ import annotations

from rabota.web import content_routes as cr

DATA = {
    "q": [
        {"label": "Бариста", "value": "Бариста", "kind": "profession", "n": 12},
        {"label": "Барменс", "value": "Барменс", "kind": "profession", "n": 3},
        {"label": "Кассир", "value": "Кассир", "kind": "profession", "n": 30},
        {"label": "Продавец-кассир", "value": "Продавец-кассир", "kind": "profession", "n": 8},
        {"label": "Администратор", "value": "Администратор", "kind": "profession", "n": 5},
    ],
    "where": [
        {"label": "Донецк", "value": "Донецк", "kind": "city", "n": 50},
        {"label": "Ворошиловский район", "value": "Ворошиловский район", "kind": "district", "n": 9},
        {"label": "Текстильщик", "value": "Текстильщик", "kind": "district", "n": 2},
    ],
}


def labels(items):
    return [i["label"] for i in items]


def test_suggest_prefix_first_then_inside_sorted_by_popularity():
    assert labels(cr.suggest(DATA, "q", "бар")) == ["Бариста", "Барменс"]
    # слово внутри названия: "касс" находит и "Кассир", и "Продавец-кассир"; начало слова выше, чем середина
    assert labels(cr.suggest(DATA, "q", "касс")) == ["Кассир", "Продавец-кассир"]
    assert labels(cr.suggest(DATA, "q", "тор")) == ["Администратор"]  # просто вхождение
    assert cr.suggest(DATA, "q", "яяя") == []


def test_suggest_empty_query_returns_most_popular_and_respects_limit():
    assert labels(cr.suggest(DATA, "q", ""))[:2] == ["Кассир", "Бариста"]
    assert len(cr.suggest(DATA, "q", "", limit=2)) == 2
    assert labels(cr.suggest(DATA, "where", ""))[0] == "Донецк"


def test_suggest_ignores_case_yo_and_extra_spaces():
    data = {"q": [{"label": "Ёлочный мастер", "value": "x", "kind": "profession", "n": 1}], "where": []}
    assert len(cr.suggest(data, "q", "  ЕЛОЧ  ")) == 1


def test_suggest_endpoint_returns_json_with_cache_headers(client, add):
    add(profession="бариста", district="Ворошиловский")
    add(profession="кассир")
    r = client.get("/suggest?f=q&q=ба")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"items": [{"label": "Бариста", "value": "Бариста", "kind": "profession", "n": 1}]}
    assert "max-age=60" in r.headers["cache-control"] and "noindex" in r.headers["x-robots-tag"]
    places = client.get("/suggest?f=where").json()["items"]
    assert {"Донецк", "Ворошиловский район"} <= {i["label"] for i in places}
    assert {i["kind"] for i in places} == {"city", "district"}


def test_suggest_endpoint_is_safe_with_odd_input(client, add):
    add(profession="бариста")
    for q in ("", "%", "'; drop table vacancies;--", "<script>", "я" * 500, "%00"):
        r = client.get("/suggest", params={"q": q, "f": "where"})
        assert r.status_code == 200 and "items" in r.json()
    assert client.get("/suggest?f=zzz&q=ба").json()["items"][0]["label"] == "Бариста"  # неизвестное поле: профессии


def test_suggest_data_is_cached_for_a_minute(client, add):
    cr.CACHE_SECONDS = 60
    add(profession="бариста")
    client.get("/suggest?f=q&q=ба")
    add(profession="баристка")
    # данные для подсказок уже собраны: новая профессия появится после истечения кэша
    labels_now = [i["label"] for i in client.get("/suggest?f=q&q=ба").json()["items"]]
    assert labels_now == ["Бариста"]


def test_suggest_cache_can_be_disabled_for_fresh_data(client, add):
    cr.CACHE_SECONDS = 0
    try:
        add(profession="бариста")
        client.get("/suggest?f=q&q=ба")
        add(profession="баристка")
        assert len(client.get("/suggest?f=q&q=ба").json()["items"]) == 2
    finally:
        cr.CACHE_SECONDS = 60


def test_pages_do_not_embed_suggestion_lists(client, add):
    for i in range(40):
        add(profession=f"профессия{i}", ago=i + 1)
    html = client.get("/").text
    assert "<datalist" not in html and "профессия39</option>" not in html
    assert html.count("data-suggest=") == 2  # поле профессии и поле «где» подключены к /suggest


def test_search_fields_have_script_hooks_and_accessible_labels(client):
    html = client.get("/vacancies").text
    assert 'data-suggest="q"' in html and 'data-suggest="where"' in html
    for fid in ("hs-q", "hs-where"):
        assert f'for="{fid}"' in html
