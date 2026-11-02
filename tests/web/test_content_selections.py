"""Подборки: условия, пороги индексации, дубли, шаблоны title и description, карта сайта и перелинковка."""
from __future__ import annotations

from urllib.parse import quote

from rabota.web import selections as sel
from rabota.web import seo as seolib
from tests.web.test_seo import Page


def many(add, n, **kw):
    for i in range(n):
        add(ago=10 + i, **kw)


def codes(client, path):
    r = client.get(path)
    return r.status_code, "noindex" in r.headers.get("x-robots-tag", "")


def test_every_selection_has_unique_slug_and_known_articles():
    from rabota.web import articles

    assert len({s.slug for s in sel.SELECTIONS}) == len(sel.SELECTIONS) == 7
    slugs = {a.slug for a in articles.all_articles()}
    for s in sel.SELECTIONS:
        assert set(s.articles) <= slugs, s.slug
        assert s.lead and s.rule and s.tips
        assert "\u2014" not in s.heading + s.lead + s.rule + "".join(s.tips)


def test_selection_conditions_match_the_right_vacancies(conn, add):
    add(profession="грузчик", employment="подработка")
    add(profession="курьер", employment="на сегодня")
    add(profession="повар", employment="постоянно", pay_daily=True)
    add(profession="кассир", experience="не нужен")
    add(profession="оператор", work_format="удалённо")
    add(profession="сварщик", work_format="вахта")
    add(profession="официант", summary="Ищем студентов на вечерние смены")
    add(profession="продавец", age_min=18)
    add(profession="уборщик", age_min=16)
    add(profession="охранник", age_min=21)
    add(profession="водитель", requirements="возраст 18+")
    add(profession="бармен", status="hidden", employment="подработка")  # скрытая не считается
    c = sel.counts(conn)
    assert c["podrabotka-na-segodnya"] == 2
    assert c["ezhednevnaya-vyplata"] == 1
    assert c["rabota-bez-opyta"] == 1
    assert c["udalennaya-rabota"] == 1 and c["vahta"] == 1
    assert c["rabota-dlya-studentov"] == 1  # только «студентов» в тексте: у подработок нет пометки «опыт не нужен»
    assert c["vakansii-18-plus"] == 3  # age_min 18, 16 и пометка 18+; age_min 21 не подходит


def test_student_selection_includes_side_jobs_without_experience(conn, add):
    add(profession="грузчик", employment="подработка", experience="не нужен")
    add(profession="курьер", employment="подработка", experience="от 1 года")
    assert sel.counts(conn)["rabota-dlya-studentov"] == 1


def test_indexable_rule_needs_threshold_and_strict_subset():
    assert not sel.is_indexable(4, None, 100)
    assert sel.is_indexable(5, None, 100)
    assert not sel.is_indexable(100, None, 100)  # подборка совпала со всей лентой
    assert sel.is_indexable(5, 9, 100)  # район больше подборки: самостоятельная страница
    assert not sel.is_indexable(9, 9, 100)  # совпала с выдачей района


def test_base_selection_below_threshold_is_noindex_but_open(client, add):
    many(add, 4, employment="подработка")
    add(profession="прочее", employment="постоянно")
    assert codes(client, "/podborki/podrabotka-na-segodnya") == (200, True)
    many(add, 1, employment="подработка")
    assert codes(client, "/podborki/podrabotka-na-segodnya") == (200, False)


def test_selection_equal_to_whole_feed_is_noindex(client, add):
    many(add, 6, employment="подработка")  # все вакансии на сайте входят в подборку: дубль ленты
    assert codes(client, "/podborki/podrabotka-na-segodnya") == (200, True)
    add(profession="прочее", employment="постоянно")
    assert codes(client, "/podborki/podrabotka-na-segodnya") == (200, False)


def test_empty_selection_shows_state_and_noindex(client):
    r = client.get("/podborki/vahta")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]
    assert "Пока нет вакансий в этой подборке" in r.text and "0 вакансий" in Page(r.text).metas["description"]


def test_unknown_selection_and_scope_are_404(client, add):
    many(add, 6, employment="подработка")
    assert client.get("/podborki/net-takoj").status_code == 404
    assert client.get("/podborki/podrabotka-na-segodnya/" + quote("Нигде")).status_code == 404
    add(profession="прочее", employment="постоянно")
    # район есть, но в нём нет вакансий подборки
    add(profession="кто-то", district="Куйбышевский", employment="постоянно")
    assert client.get("/podborki/vahta/" + quote("Куйбышевский")).status_code == 404


def test_combo_with_district_and_profession(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 3, profession="кассир", district="Ворошиловский", employment="постоянно")
    d = client.get("/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский"))
    page = Page(d.text)
    assert d.status_code == 200 and "x-robots-tag" not in d.headers
    assert page.h1 == ["Подработка на сегодня: Ворошиловский район"]
    assert page.links["canonical"] == "https://example.test/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский")
    assert "6 вакансий" in page.title or "6\xa0вакансий" in page.title
    # профессия целиком совпадает с подборкой: дубль профессии, закрыто
    p = client.get("/podborki/podrabotka-na-segodnya/" + quote("бариста"))
    assert p.status_code == 200 and "noindex" in p.headers["x-robots-tag"]
    # регистр из адреса приводится к записи в базе
    low = Page(client.get("/podborki/podrabotka-na-segodnya/" + quote("ворошиловский")).text)
    assert low.links["canonical"] == page.links["canonical"]


def test_titles_and_descriptions_fit_length_rules(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка", pay_min=30000, pay_max=40000,
         pay_period="месяц")
    many(add, 3, profession="кассир", district="Ворошиловский", employment="постоянно")
    for path in ("/podborki", "/podborki/podrabotka-na-segodnya", "/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский")):
        page = Page(client.get(path).text)
        assert 10 <= len(page.title) <= seolib.TITLE_LIMIT + 8, (path, page.title)
        desc = page.metas["description"]
        assert seolib.DESC_MIN <= len(desc) <= seolib.DESC_MAX, (path, desc)
        assert "\u2014" not in page.title + desc
        assert page.metas["og:title"] == page.title and page.links["canonical"].startswith("https://example.test/")


def test_pagination_has_canonical_and_prev_next(client, add):
    many(add, 25, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 3, profession="кассир", employment="постоянно")
    p1 = Page(client.get("/podborki/podrabotka-na-segodnya").text)
    p2 = Page(client.get("/podborki/podrabotka-na-segodnya?page=2").text)
    assert p1.links["canonical"].endswith("/podborki/podrabotka-na-segodnya")
    assert p2.links["canonical"].endswith("?page=2") and p2.links["prev"].endswith("podrabotka-na-segodnya")
    assert p1.title != p2.title and p1.metas["description"] != p2.metas["description"]
    assert "next" in p1.links


def test_breadcrumbs_and_json_ld(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 3, profession="кассир", district="Ворошиловский", employment="постоянно")
    page = Page(client.get("/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский")).text)
    import json

    crumbs = json.loads(page.ld[-1])["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Главная", "Подборки", "Подработка на сегодня", "Ворошиловский район"]
    assert json.loads(page.ld[0])["@type"] == "ItemList"


def test_internal_links_from_selection_to_siblings_and_articles(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка", experience="не нужен")
    many(add, 3, profession="кассир", district="Ворошиловский", employment="постоянно")
    html = client.get("/podborki/podrabotka-na-segodnya").text
    assert 'href="/podborki/rabota-bez-opyta"' in html  # другая подборка с вакансиями
    assert 'href="/podborki/vahta"' not in html  # пустые подборки не рекламируем
    assert 'href="/podborki/podrabotka-na-segodnya/' + quote("Ворошиловский", safe="") + '"' in html
    assert 'href="/blog/kak-otlichit-moshennikov"' in html  # читайте также


def test_profession_and_district_pages_link_to_selections(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 4, profession="бариста", district="Калининский", employment="постоянно")
    many(add, 3, profession="бариста", district="Ворошиловский", employment="постоянно")
    html = client.get("/profession/" + quote("бариста")).text
    assert "Подборки по профессии" in html
    assert 'href="/podborki/podrabotka-na-segodnya/' + quote("бариста", safe="") + '"' in html
    district = client.get("/district/" + quote("Ворошиловский")).text
    assert "Подборки в этом районе" in district
    assert 'href="/podborki/podrabotka-na-segodnya/' + quote("Ворошиловский", safe="") + '"' in district


def test_sitemap_has_only_indexable_selection_pages(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 3, profession="кассир", district="Калининский", employment="постоянно")
    xml = client.get("/sitemap.xml").text
    assert "/podborki/podrabotka-na-segodnya</loc>" in xml
    assert "/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский") not in xml  # район целиком совпал с подборкой: дубль
    assert "/podborki/vahta" not in xml
    assert "/podborki</loc>" in xml


def test_district_combo_in_sitemap_when_strict_subset(client, add):
    many(add, 6, profession="бариста", district="Ворошиловский", employment="подработка")
    many(add, 3, profession="кассир", district="Ворошиловский", employment="постоянно")
    xml = client.get("/sitemap.xml").text
    assert "/podborki/podrabotka-na-segodnya/" + quote("Ворошиловский") in xml
    assert "/podborki/podrabotka-na-segodnya/" + quote("бариста") not in xml  # профессия целиком, дубль


def test_quick_chips_on_home_only_for_non_empty_selections(client, add):
    from rabota.web import content_routes

    content_routes.CACHE_SECONDS = 0
    try:
        many(add, 3, employment="подработка")
        html = client.get("/").text
        assert 'href="/podborki/podrabotka-na-segodnya"' in html and 'href="/podborki/vahta"' not in html
    finally:
        content_routes.CACHE_SECONDS = 60


def test_index_page_lists_all_selections_and_counts(client, add):
    many(add, 6, employment="подработка")
    add(profession="прочее", employment="постоянно")
    html = client.get("/podborki").text
    for s in sel.SELECTIONS:
        assert f'href="/podborki/{s.slug}"' in html
    assert "Пока пусто" in html  # пустые подборки подписаны честно
    assert "x-robots-tag" not in client.get("/podborki").headers  # есть подборка с достаточным числом вакансий


def test_selection_pages_are_server_rendered(client, add):
    many(add, 6, employment="подработка", profession="бариста")
    html = client.get("/podborki/podrabotka-na-segodnya").text
    assert html.count('class="vcard') >= 5 and "<noscript" not in html
