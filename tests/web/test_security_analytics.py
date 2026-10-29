"""Метрика и Вебмастер: подключаются только переменными, счётчик только после согласия."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rabota.web import analytics
from rabota.web.app import create_app

STATIC = Path(__file__).resolve().parents[2] / "rabota" / "web" / "static"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def build(**env):
        for name in ("METRIKA_ID", "METRIKA_WEBVISOR", "WEBMASTER_VERIFY_FILE", "WEBMASTER_VERIFY_BODY"):
            monkeypatch.delenv(name, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return TestClient(create_app(tmp_path / "a.db", site_url="https://example.test"))

    return build


def test_without_id_nothing_is_inserted(make_client):
    c = make_client()
    html = c.get("/").text
    assert "consent.js" not in html and "cookie-consent" not in html and "mc.yandex" not in html
    assert "yandex" not in c.get("/").headers["content-security-policy"]


def test_banner_and_loader_appear_with_id(make_client):
    c = make_client(METRIKA_ID="12345678")
    html = c.get("/").text
    assert 'id="cookie-consent"' in html and "hidden" in html.split('id="cookie-consent"', 1)[1][:200]
    assert 'data-metrika-id="12345678"' in html
    assert 'data-consent="accept"' in html and 'data-consent="reject"' in html
    assert "/legal/cookies" in html
    # сам код счётчика в HTML не вставляется, он грузится скриптом после согласия
    assert "mc.yandex.ru/metrika" not in html and "ym(" not in html
    assert html.index("consent.js") > html.index('class="site-footer"')
    assert "mc.yandex.ru" in c.get("/").headers["content-security-policy"]


def test_banner_also_on_other_templates(make_client):
    c = make_client(METRIKA_ID="1")
    for path in ("/login", "/legal/cookies", "/about"):
        assert "cookie-consent" in c.get(path).text, path


def test_non_numeric_id_is_ignored(make_client):
    c = make_client(METRIKA_ID="abc; alert(1)")
    assert "consent.js" not in c.get("/").text


def test_webvisor_flag(make_client):
    assert 'data-webvisor="1"' in make_client(METRIKA_ID="5").get("/").text
    assert 'data-webvisor="0"' in make_client(METRIKA_ID="5", METRIKA_WEBVISOR="0").get("/").text


def test_loader_script_never_loads_counter_before_consent():
    js = (STATIC / "consent.js").read_text(encoding="utf-8")
    # адрес счётчика встречается один раз, внутри функции загрузки
    assert js.count("mc.yandex.ru/metrika/tag.js") == 1
    body = js[js.index("function loadMetrika"):]
    assert body.index("tag.js") < body.index("function dropCookies")
    # загрузка вызывается только из выбора "принять" и из сохранённого согласия
    assert js.count("loadMetrika()") == 3  # определение не считается: два вызова + выбор
    assert "saved.analytics" in js and "analytics) loadMetrika" in js


def test_loader_has_revoke_and_footer_link():
    js = (STATIC / "consent.js").read_text(encoding="utf-8")
    assert "Настройки cookie" in js and "dropCookies" in js and "rabota_consent" in js
    css = (STATIC / "consent.css").read_text(encoding="utf-8")
    assert "var(--c-surface)" in css and "prefers-reduced-motion" in css
    # только токены: ни одного жёстко заданного цвета
    import re

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)


def test_webmaster_verification_file(make_client):
    c = make_client(WEBMASTER_VERIFY_FILE="yandex_0123abcd.html")
    r = c.get("/yandex_0123abcd.html")
    assert r.status_code == 200 and "Verification: 0123abcd" in r.text
    assert r.headers["content-type"].startswith("text/html")
    assert make_client().get("/yandex_0123abcd.html").status_code == 404


def test_webmaster_custom_body_and_bad_names(make_client):
    c = make_client(WEBMASTER_VERIFY_FILE="google123.html", WEBMASTER_VERIFY_BODY="google-site-verification: google123.html")
    assert c.get("/google123.html").text == "google-site-verification: google123.html"
    for bad in ("../etc/passwd", "a/b.html", "x.php", ".env"):
        assert analytics.from_env({"WEBMASTER_VERIFY_FILE": bad}).verify_name == ""


def test_from_env_defaults():
    a = analytics.from_env({})
    assert not a.enabled and a.webvisor and a.verify_name == ""
