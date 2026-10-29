"""Заголовки безопасности и cookie на настоящем приложении."""
from __future__ import annotations

from fastapi.testclient import TestClient

from rabota.web.app import create_app


def test_pages_carry_security_headers(client):
    for path in ("/", "/vacancies", "/login", "/robots.txt", "/no-such-page"):
        r = client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["x-frame-options"] == "DENY", path
        assert "content-security-policy" in r.headers, path
        assert "permissions-policy" in r.headers, path
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin", path


def test_hsts_follows_forwarded_proto(client):
    plain = client.get("/")
    assert "strict-transport-security" not in plain.headers
    secure = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert secure.headers["strict-transport-security"].startswith("max-age=")


def test_csp_has_no_analytics_hosts_without_metrika(client):
    assert "yandex" not in client.get("/").headers["content-security-policy"]


def test_static_is_cached_for_long_when_versioned(client):
    html = client.get("/").text
    import re

    path = re.search(r'(/static/tokens\.css\?v=\w+)', html).group(1)
    r = client.get(path)
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]


def test_cookies_are_httponly_samesite_and_secure_behind_https_proxy(db_path):
    app = create_app(db_path, site_url="https://example.test")
    with TestClient(app, base_url="http://testserver") as c:
        plain = c.get("/login").headers["set-cookie"].lower()
    with TestClient(app, base_url="http://testserver") as c:
        secure = c.get("/login", headers={"X-Forwarded-Proto": "https"}).headers["set-cookie"].lower()
    for header in (plain, secure):
        assert "httponly" in header and "samesite=lax" in header
    assert "secure" not in plain.replace("samesite", "")
    assert "; secure" in secure


def test_error_page_does_not_leak_traceback(db_path):
    app = create_app(db_path, site_url="https://example.test")

    @app.get("/boom-for-test")
    def boom():
        raise RuntimeError("секретная подробность")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom-for-test")
    assert r.status_code == 500
    assert "секретная подробность" not in r.text and "Traceback" not in r.text
