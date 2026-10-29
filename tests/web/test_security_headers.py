"""Заголовки безопасности и кеш статики: ASGI-прослойка на минимальном приложении (без остального сайта)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from rabota.web import security


def make_app(tmp_path, *, site_url="https://example.test", metrika=False) -> FastAPI:
    app = FastAPI()
    static = tmp_path / "static"
    static.mkdir()
    (static / "a.css").write_text("body{}", encoding="utf-8")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return PlainTextResponse("ok")

    @app.get("/own")
    def own():
        return PlainTextResponse("x", headers={"Content-Security-Policy": "default-src 'none'", "Referrer-Policy": "no-referrer"})

    security.install_security(app, site_url=site_url, metrika=metrika)
    return app


@pytest.fixture
def client(tmp_path):
    with TestClient(make_app(tmp_path), base_url="https://example.test") as c:
        yield c


def test_basic_headers_present(client):
    h = client.get("/").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert h["cross-origin-opener-policy"] == "same-origin"
    pp = h["permissions-policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert feature in pp


def test_csp_is_strict_by_default(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "yandex" not in csp
    assert "upgrade-insecure-requests" in csp


def test_csp_with_metrika_allows_only_yandex_hosts(tmp_path):
    with TestClient(make_app(tmp_path, metrika=True), base_url="https://example.test") as c:
        csp = c.get("/").headers["content-security-policy"]
    assert "https://mc.yandex.ru" in csp
    script = next(p for p in csp.split("; ") if p.startswith("script-src"))
    assert "'unsafe-inline'" not in script and "*" not in script
    assert "wss://mc.yandex.ru" in csp
    assert "frame-ancestors 'none'" in csp


def test_hsts_only_over_https(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://example.test") as c:
        assert "strict-transport-security" not in c.get("/").headers
        forwarded = c.get("/", headers={"X-Forwarded-Proto": "https"}).headers
        assert forwarded["strict-transport-security"] == "max-age=31536000"
    with TestClient(app, base_url="https://example.test") as c:
        h = c.get("/").headers["strict-transport-security"]
        assert h.startswith("max-age=") and "preload" not in h and "includeSubDomains" not in h


def test_no_hsts_when_site_is_plain_http(tmp_path):
    app = make_app(tmp_path, site_url="http://127.0.0.1:8000")
    with TestClient(app, base_url="https://example.test") as c:
        h = c.get("/").headers
    assert "strict-transport-security" not in h
    assert "upgrade-insecure-requests" not in h["content-security-policy"]


def test_handler_headers_are_not_overwritten(client):
    h = client.get("/own").headers
    assert h["content-security-policy"] == "default-src 'none'"
    assert h["referrer-policy"] == "no-referrer"


def test_static_cache_rules(client):
    versioned = client.get("/static/a.css?v=123")
    plain = client.get("/static/a.css")
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert plain.headers["cache-control"] == "public, max-age=604800"
    assert "cache-control" not in client.get("/").headers or "immutable" not in client.get("/").headers["cache-control"]
    missing = client.get("/static/none.css?v=1")
    assert missing.status_code == 404 and "immutable" not in missing.headers.get("cache-control", "")


def test_headers_on_error_responses(client):
    r = client.get("/no-such-path")
    assert r.status_code == 404
    assert r.headers["x-content-type-options"] == "nosniff"


def test_build_csp_pure():
    assert "mc.yandex" not in security.build_csp()
    assert security.build_csp(upgrade=True).endswith("upgrade-insecure-requests")
