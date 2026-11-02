"""Индексация страниц работодателей и карта сайта: автокарточка открыта только при достаточных вакансиях или проверке."""
from __future__ import annotations

import re

import pytest

from rabota.employers_auto import seo
from rabota.web import queries as q
from tests.accounts.harness import build_env
from tests.employers_auto.kit import NOW, auto_company, vacancy


def test_is_indexable_rules():
    auto = {"origin": "auto"}
    assert not seo.is_indexable(auto, 2, 0, 3)
    assert seo.is_indexable(auto, 2, 1, 3), "архив тоже считается"
    assert seo.is_indexable({"origin": "auto", "verified_at": "2026-10-01"}, 0, 1, 3)
    assert not seo.is_indexable({"origin": "auto", "verified_at": "2026-10-01"}, 0, 0, 3)
    assert not seo.is_indexable({"origin": "auto", "blocked_at": "2026-10-01"}, 9, 9, 3)
    assert not seo.is_indexable({"origin": "auto", "merged_into": 5}, 9, 9, 3)
    assert seo.is_indexable({"origin": "registered"}, 1, 0, 3)
    assert not seo.is_indexable({"origin": "registered"}, 0, 5, 3)
    assert not seo.is_indexable(None, 5, 5)


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        yield e


def robots(html: str) -> str:
    m = re.search(r'<meta name="robots" content="([^"]*)"', html)
    return m.group(1) if m else ""


def jsonld(html: str) -> list[str]:
    return re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.DOTALL)


def test_thin_auto_card_is_noindex_and_not_in_sitemap(env):
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=2)
    finally:
        conn.close()
    page = env.client.get(f"/company/{cid}")
    assert page.status_code == 200
    assert "noindex" in robots(page.text) and "follow" in robots(page.text)
    assert f"/company/{cid}<" not in env.client.get("/sitemap.xml").text


def test_rich_auto_card_is_indexable_and_in_sitemap(env):
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=3)
    finally:
        conn.close()
    page = env.client.get(f"/company/{cid}")
    assert "noindex" not in robots(page.text)
    assert f"/company/{cid}</loc>" in env.client.get("/sitemap.xml").text


def test_verified_card_with_one_vacancy_is_indexable(env):
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=1)
        conn.execute("UPDATE companies SET verified_at=? WHERE id=?", (NOW.isoformat(), cid))
        conn.commit()
    finally:
        conn.close()
    assert "noindex" not in robots(env.client.get(f"/company/{cid}").text)


def test_archive_counts_towards_threshold(env):
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=3)
        conn.execute("UPDATE vacancies SET status='archived' WHERE company_id=? AND id IN "
                     "(SELECT id FROM vacancies WHERE company_id=? LIMIT 2)", (cid, cid))
        conn.commit()
    finally:
        conn.close()
    assert "noindex" not in robots(env.client.get(f"/company/{cid}").text)


def test_threshold_follows_setting(env):
    from rabota.admin import settings

    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=2)
        settings.save(conn, {"id": 1, "email": "a@example.test", "role": "admin"}, {"employer_index_min": "2"}, now=NOW)
    finally:
        conn.close()
    assert "noindex" not in robots(env.client.get(f"/company/{cid}").text)


def test_jsonld_has_no_invented_data(env):
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=3, city=None)
    finally:
        conn.close()
    html = env.client.get(f"/company/{cid}").text
    blocks = [b for b in jsonld(html) if '"Organization"' in b]
    assert blocks
    for b in blocks:
        # адреса, сайта и соцсетей нет в источнике: их в разметке тоже нет; телефон только из самой вакансии
        assert "address" not in b.lower() and "sameAs" not in b and '"description"' not in b
        assert "+79490001122" in b


def test_merged_card_redirects_to_main(env):
    conn = env.db()
    try:
        main = auto_company(conn, "Кофейня Зерно", count=3)
        dup = auto_company(conn, "Зерно Кофейня", count=1, source="@x")
        conn.execute("UPDATE companies SET merged_into=? WHERE id=?", (main, dup))
        conn.commit()
    finally:
        conn.close()
    r = env.client.get(f"/company/{dup}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"].endswith(f"/company/{main}")


def test_sitemap_helper_matches_threshold(tmp_path):
    conn = q.open_conn(tmp_path / "s.db")
    try:
        thin = auto_company(conn, "Пекарня Хлеб", count=2)
        rich = auto_company(conn, "Кофейня Зерно", count=3, source="@y")
        ids = {i for i, _ in q.sitemap_entries(conn)["companies"]}
        assert rich in ids and thin not in ids
    finally:
        conn.close()
    assert vacancy
