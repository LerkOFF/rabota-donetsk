"""Блог: загрузка статей из docs/articles, отрисовка Markdown на сервере, оглавление, JSON-LD Article, перелинковка."""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from rabota.web import articles
from tests.web.test_seo import Page

SLUGS = {
    "kak-sostavit-rezyume", "kak-projti-sobesedovanie", "kak-otlichit-moshennikov", "samozanyatost-v-dnr",
    "dokumenty-dlya-trudoustroystva-v-dnr", "podrabotka-studentu", "kak-iskat-rabotu-cherez-telegram-i-sayt",
    "grafik-2-2-i-smennye-grafiki",
}
FORBIDDEN = ("obsi" + "dian", "va" + "ult", "cla" + "ude", "cur" + "sor", "co" + "dex", "аг" + "ент", "/us" + "ers/", "~/.", "pa" + "ss/")


def test_eight_articles_are_loaded_with_required_fields():
    items = articles.all_articles()
    assert {a.slug for a in items} == SLUGS and len(items) == 8
    for a in items:
        assert 10 <= len(a.title) <= 90 and 90 <= len(a.description) <= 200, a.slug
        assert a.words >= 450, f"{a.slug}: слишком короткая статья ({a.words} слов)"
        assert len(a.toc) >= 3 and a.minutes >= 2
        assert a.published <= a.updated and isinstance(a.published, date)
        assert set(a.related) <= SLUGS - {a.slug}, a.slug
        assert a.search_url.startswith("/"), a.slug


def test_articles_have_no_forbidden_words_dashes_or_made_up_links():
    for a in articles.all_articles():
        text = (a.title + a.description + a.body).lower()
        assert "\u2014" not in text, f"{a.slug}: длинное тире"
        for word in FORBIDDEN:
            assert word not in text, f"{a.slug}: {word}"
        # ссылок на законы и на внешние сайты в статьях нет: только внутренние адреса
        assert not re.search(r"https?://", a.body), a.slug
        assert not re.search(r"\b(ст\.|статья)\s*\d+|№\s*\d+-фз|\d+-фз", text), f"{a.slug}: ссылка на закон"
        for link in re.findall(r"\]\((/[^)]*)\)", a.body):
            assert link.startswith("/"), link


def test_internal_links_in_articles_point_to_real_pages(client):
    for a in articles.all_articles():
        for link in {a.search_url, *re.findall(r"\]\((/[^)\s]*)\)", a.body)}:
            path = link.split("#")[0].split("?")[0]
            assert client.get(path or "/").status_code == 200, link


def test_render_markdown_escapes_html_and_blocks_unsafe_links():
    html, toc, words = articles.render_markdown(
        "## Заголовок <b>\n\nТекст <script>alert(1)</script> и [ссылка](javascript:alert(1)) и [ок](/blog) **жирный** *курсив*\n\n"
        "- один\n- два\n\n1. первый\n2. второй\n\n> Важно\n")
    assert "<script>" not in html and "&lt;script&gt;" in html and "<b>" not in html
    assert "javascript:" not in html and 'href="/blog"' in html
    assert "<strong>жирный</strong>" in html and "<em>курсив</em>" in html
    assert "<ul" in html and "<ol" in html and 'class="callout"' in html
    assert toc == [("s1", "Заголовок &lt;b&gt;")] and words > 10


def test_parse_reports_broken_metadata():
    with pytest.raises(ValueError, match="нет блока"):
        articles.parse("просто текст", "a.md")
    with pytest.raises(ValueError, match="не закрыт"):
        articles.parse("---\nslug: a\n", "a.md")
    with pytest.raises(ValueError, match="нет поля title"):
        articles.parse("---\nslug: a\ndescription: d\npublished: 2026-11-02\n---\nтекст", "a.md")
    with pytest.raises(ValueError, match="slug"):
        articles.parse("---\nslug: Плохой Адрес\ntitle: t\ndescription: d\npublished: 2026-11-02\n---\nтекст", "a.md")


def test_broken_file_is_skipped_not_fatal(tmp_path):
    (tmp_path / "ok.md").write_text("---\nslug: ok\ntitle: Нормальная статья\ndescription: Описание\npublished: 2026-11-02\n---\n## Раздел\n\nТекст.", encoding="utf-8")
    (tmp_path / "bad.md").write_text("без метаданных", encoding="utf-8")
    articles._load.cache_clear()
    try:
        assert [a.slug for a in articles.all_articles(tmp_path)] == ["ok"]
    finally:
        articles._load.cache_clear()


def test_related_prefers_declared_then_fills_up():
    a = articles.get("kak-sostavit-rezyume")
    more = articles.related(a)
    assert [m.slug for m in more[:2]] == list(a.related[:2]) and len(more) == 3 and a not in more


def test_blog_index_lists_all_articles(client):
    r = client.get("/blog")
    page = Page(r.text)
    assert r.status_code == 200 and page.h1 == ["Блог о работе в Донецке"]
    for slug in SLUGS:
        assert f'href="/blog/{slug}"' in r.text
    assert json.loads(page.ld[0])["@type"] == "BreadcrumbList"


def test_article_page_is_server_rendered_with_toc_json_ld_and_related(client):
    r = client.get("/blog/kak-otlichit-moshennikov")
    page = Page(r.text)
    assert r.status_code == 200 and len(page.h1) == 1
    assert 'class="toc"' in r.text and 'href="#s1"' in r.text and 'id="s1"' in r.text
    assert "Читайте также" in r.text and r.text.count('class="acard"') == 3
    assert 'class="cta"' in r.text and "<noscript" not in r.text
    article = json.loads(page.ld[0])
    assert article["@type"] == "Article" and article["author"] == {
        "@type": "Organization", "name": "Работа Донецк", "url": "https://example.test/"}
    assert article["datePublished"] == "2026-11-02" and article["dateModified"] >= article["datePublished"]
    assert article["mainEntityOfPage"] == "https://example.test/blog/kak-otlichit-moshennikov"
    assert article["publisher"] == {"@id": "https://example.test/#organization"}
    assert page.metas["og:type"] == "article" and "x-robots-tag" not in r.headers


def test_unknown_article_is_404(client):
    assert client.get("/blog/net-takoj-stati").status_code == 404


def test_articles_are_in_sitemap(client):
    xml = client.get("/sitemap.xml").text
    assert "https://example.test/blog</loc>" in xml
    for slug in SLUGS:
        assert f"/blog/{slug}</loc>" in xml


def test_home_links_to_latest_articles_and_salary(client):
    html = client.get("/").text
    assert 'href="/zarplaty"' in html and "Полезное для соискателей" in html
    assert 'href="/blog/' in html
