"""IndexNow: файл ключа, пакет адресов, состояние между запусками."""
from __future__ import annotations

import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rabota import config
from rabota.db import connect
from rabota.ops import indexnow
from rabota.web.indexnow import install_indexnow

KEY = "0123456789abcdef0123456789abcdef"


def test_key_file_is_served_only_with_key():
    app = FastAPI()
    install_indexnow(app, "")
    assert TestClient(app).get(f"/{KEY}.txt").status_code == 404
    app = FastAPI()
    install_indexnow(app, KEY)
    resp = TestClient(app).get(f"/{KEY}.txt")
    assert resp.status_code == 200 and resp.text == KEY


def test_key_validation(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "короткий!")
    assert indexnow.key() == ""
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    assert indexnow.key() == KEY
    monkeypatch.setenv("INDEXNOW_KEY", "")
    assert indexnow.key() == ""


def test_payload_has_host_key_location_and_urls():
    payload = indexnow.build_payload("https://работа-донецк.рф", KEY, [5, 7])
    assert payload["host"] == "работа-донецк.рф"
    assert payload["keyLocation"] == f"https://работа-донецк.рф/{KEY}.txt"
    assert payload["urlList"] == [
        "https://работа-донецк.рф/", "https://работа-донецк.рф/vacancies",
        "https://работа-донецк.рф/vacancy/5", "https://работа-донецк.рф/vacancy/7",
    ]


def _seed(path):
    c = connect(path)
    for name, city, status in (("а", "Донецк", "active"), ("б", "Мариуполь", "active"), ("в", None, "active"),
                               ("г", "Донецк", "expired")):
        c.execute("INSERT INTO vacancies(profession, city, kind, status) VALUES (?, ?, 'vacancy', ?)", (name, city, status))
    c.commit()
    c.close()


def test_run_sends_only_visible_new_vacancies_and_remembers(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.setattr(indexnow.queries, "open_conn", lambda: connect(db))
    monkeypatch.setattr(indexnow.queries, "VISIBLE_SQL", indexnow.queries.city_scope_sql(["Донецк"]) + " AND kind='vacancy' AND status='active'")
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert indexnow.run(client=client) == 2  # Донецк и без города; Мариуполь и закрытая не уходят
    assert [u for u in sent[0]["urlList"] if "/vacancy/" in u] == ["https://example.test/vacancy/1", "https://example.test/vacancy/3"]
    assert indexnow.run(client=client) == 0  # повторно ничего нового
    assert len(sent) == 1


def test_failed_submit_keeps_state(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _seed(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    monkeypatch.setattr(indexnow.queries, "open_conn", lambda: connect(db))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    assert indexnow.run(client=client) == 0
    assert indexnow.load_last_id(tmp_path / "indexnow-state.json") == 0


def test_no_key_does_nothing(monkeypatch):
    monkeypatch.setenv("INDEXNOW_KEY", "")
    assert indexnow.run() == 0
