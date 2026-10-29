"""Маршруты /legal: список, документы, 404, оглавление, заглушки."""
import re

import pytest
from fastapi.testclient import TestClient

from rabota import legal
from rabota.web import legal_routes
from rabota.web.app import create_app
from tests.web.conftest import NOW

SLUGS = [d.slug for d in legal.list_documents()]
OPERATOR_ENV = ("OPERATOR_NAME", "OPERATOR_STATUS", "OPERATOR_INN", "OPERATOR_EMAIL", "OPERATOR_ADDRESS")


@pytest.fixture(autouse=True)
def operator_env(monkeypatch):
    for name in (*OPERATOR_ENV, "SITE_NAME", "LEGAL_UPDATED_AT"):
        monkeypatch.delenv(name, raising=False)
    legal.reset_warnings()


@pytest.fixture
def legal_client(db_path):
    app = create_app(db_path, site_url="https://example.test", now=lambda: NOW)
    legal_routes.install(app, site_url="https://example.test")
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def test_index_lists_all_documents(legal_client):
    r = legal_client.get("/legal")
    assert r.status_code == 200
    assert "<h1" in r.text and "Правила и документы" in r.text
    for slug in SLUGS:
        assert f'href="/legal/{slug}"' in r.text
    assert '<link rel="canonical" href="https://example.test/legal">' in r.text


@pytest.mark.parametrize("slug", SLUGS)
def test_each_document_page_renders(legal_client, slug):
    r = legal_client.get(f"/legal/{slug}")
    assert r.status_code == 200
    assert "Черновик, требует проверки юристом" in r.text
    assert f'<link rel="canonical" href="https://example.test/legal/{slug}">' in r.text
    assert 'aria-label="Содержание"' in r.text
    assert 'aria-current="page"' in r.text
    assert "{{" not in r.text


@pytest.mark.parametrize("slug", SLUGS)
def test_toc_links_match_section_ids(legal_client, slug):
    html = legal_client.get(f"/legal/{slug}").text
    toc = re.search(r'<nav class="panel" aria-label="Содержание">(.*?)</nav>', html, re.DOTALL)
    assert toc
    anchors = re.findall(r'href="#([^"]+)"', toc.group(1))
    assert anchors
    for anchor in anchors:
        assert f'id="{anchor}"' in html, anchor
    assert len(set(anchors)) == len(anchors)


def test_unknown_document_is_404_with_site_error_page(legal_client):
    r = legal_client.get("/legal/nope")
    assert r.status_code == 404
    assert "Страницы нет" in r.text


@pytest.mark.parametrize("path", ["/legal/..%2FREADME", "/legal/privacy.md", "/legal/CHECKLIST", "/legal/%3Cscript%3E"])
def test_odd_paths_are_404(legal_client, path):
    assert legal_client.get(path).status_code == 404


def test_placeholders_visible_when_settings_missing(legal_client, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="rabota.legal"):
        r = legal_client.get("/legal/privacy")
    assert "[укажите ИНН оператора]" in r.text
    assert "[укажите почту для обращений]" in r.text
    assert any("OPERATOR_EMAIL" in rec.getMessage() for rec in caplog.records)


def test_values_from_environment_are_rendered_and_escaped(legal_client, monkeypatch):
    monkeypatch.setenv("OPERATOR_NAME", "Тестов <b>Тест</b>")
    monkeypatch.setenv("OPERATOR_STATUS", "самозанятый")
    monkeypatch.setenv("OPERATOR_INN", "000000000000")
    monkeypatch.setenv("OPERATOR_EMAIL", "legal@example.test")
    monkeypatch.setenv("OPERATOR_ADDRESS", "Тестовая улица, 1")
    html = legal_client.get("/legal/terms").text
    assert "Тестов &lt;b&gt;Тест&lt;/b&gt;" in html
    assert "<b>Тест</b>" not in html
    assert "legal@example.test" in html
    assert "[укажите" not in html


def test_page_uses_site_layout(legal_client):
    html = legal_client.get("/legal/cookies").text
    assert "site-header" in html and "site-footer" in html
    assert "/static/tokens.css" in html
    assert '<meta name="robots"' not in html
    assert "noindex" not in html


def test_install_without_site_url_uses_environment_default(db_path):
    app = create_app(db_path, now=lambda: NOW)
    legal_routes.install(app)
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/legal/privacy").status_code == 200


def test_source_documents_cover_vacancy_removal(legal_client):
    html = legal_client.get("/legal/sources").text
    assert "Оригинал" in html
    assert "3 рабочих дней" in html
