"""Сами документы: загружаются, отрисовываются, без внутренней кухни и длинного тире."""
import re
from pathlib import Path

import pytest

from rabota import legal

SLUGS = ["privacy", "terms", "employers", "consent", "dissemination", "cookies", "requests", "sources"]
VARS = {
    "operator_name": "Тестов Тест", "operator_status": "самозанятый", "operator_inn": "000000000000",
    "operator_email": "legal@example.test", "operator_address": "Тестовая улица, 1",
    "site_url": "https://example.test", "site_name": "Тест-сайт", "updated_at": "01.01.2030",
}
FORBIDDEN = ("claude", "cursor", "codex", "агент", "скилл", "vault", "obsidian", "/users/", "—")
LEGAL_FILES = sorted(legal.LEGAL_DIR.glob("*.md"))


def test_expected_documents_are_registered():
    assert [d.slug for d in legal.list_documents()] == SLUGS
    assert legal.get_document("nope") is None


@pytest.mark.parametrize("slug", SLUGS)
def test_document_loads_and_renders(slug):
    r = legal.render_document(slug, VARS)
    assert r.title
    assert len(r.html) > 500
    assert r.toc, "у документа нет разделов"
    assert legal.document_title(slug)


@pytest.mark.parametrize("slug", SLUGS)
def test_document_starts_with_draft_note(slug):
    lines = [ln for ln in legal.source_text(slug).splitlines() if ln.strip()]
    assert lines[0].startswith("# ")
    assert "Черновик, требует проверки юристом" in lines[1]
    r = legal.render_document(slug, VARS)
    assert r.html.startswith('<p class="notice notice--warn">')


@pytest.mark.parametrize("slug", SLUGS)
def test_variables_are_substituted_everywhere(slug):
    html = legal.render_document(slug, VARS).html
    assert "{{" not in html and "}}" not in html
    assert "[укажите" not in html


@pytest.mark.parametrize("slug", SLUGS)
def test_only_known_variables_used(slug):
    used = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", legal.source_text(slug)))
    assert used <= set(VARS)


def test_operator_data_appears_in_key_documents():
    for slug in ("privacy", "terms", "consent"):
        html = legal.render_document(slug, VARS).html
        assert "Тестов Тест" in html
        assert "legal@example.test" in html
        assert "000000000000" in html


@pytest.mark.parametrize("slug", SLUGS)
def test_internal_links_point_to_existing_documents_and_sections(slug):
    html = legal.render_document(slug, VARS).html
    for href in re.findall(r'href="(/legal/[^"]*)"', html):
        assert href.removeprefix("/legal/") in SLUGS, href


@pytest.mark.parametrize("path", LEGAL_FILES, ids=lambda p: p.name)
def test_source_files_have_no_forbidden_words_or_dashes(path):
    text = path.read_text(encoding="utf-8").lower()
    for word in FORBIDDEN:
        assert word not in text, f"{path.name}: {word!r}"


@pytest.mark.parametrize("path", LEGAL_FILES, ids=lambda p: p.name)
def test_source_files_have_no_personal_data(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "реальная почта в тексте"
    assert not re.search(r"\+?\d[\d\s()-]{9,}\d", text), "телефон или ИНН в тексте"


def test_legal_module_and_routes_have_no_forbidden_words():
    root = Path(__file__).resolve().parents[2]
    for rel in ("rabota/legal.py", "rabota/web/legal_routes.py", "rabota/web/templates/legal/page.html"):
        text = (root / rel).read_text(encoding="utf-8").lower()
        for word in FORBIDDEN:
            assert word not in text, f"{rel}: {word!r}"


def test_checklist_exists_and_says_it_is_not_legal_advice():
    text = (legal.LEGAL_DIR / "CHECKLIST.md").read_text(encoding="utf-8")
    assert "не юридическая консультация" in text.lower() or "не заменяют юридическую консультацию" in text.lower()
    assert "Роскомнадзор" in text


def test_privacy_covers_required_topics():
    text = legal.source_text("privacy")
    for needle in ("Сроки хранения", "Ваши права", "трансграничная передача", "территории Российской Федерации", "cookie", "Контакты оператора"):
        assert needle.lower() in text.lower(), needle


def test_terms_cover_prohibitions_and_military_recruitment():
    text = legal.source_text("terms").lower()
    for needle in ("предоплата", "закладки", "дискриминац", "военную службу", "отдельном разделе", "блокировк", "жалоб"):
        assert needle in text, needle


def test_consent_text_is_html_without_page_title():
    html = legal.consent_text(VARS)
    assert "<h1" not in html
    assert 'id="consent-1"' in html
    assert "Тестов Тест" in html
    assert "{{" not in html


def test_consent_text_uses_environment_placeholders(monkeypatch):
    for name in ("OPERATOR_NAME", "OPERATOR_STATUS", "OPERATOR_INN", "OPERATOR_EMAIL", "OPERATOR_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    assert "[укажите " in legal.consent_text()


def test_unknown_document_raises():
    with pytest.raises(legal.UnknownDocument):
        legal.render_document("nope")
