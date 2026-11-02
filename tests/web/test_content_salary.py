"""Зарплаты: чистые функции расчёта (медиана, выбросы, порог) и страницы /zarplaty, блок на странице профессии."""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

import pytest

from rabota.web import salary
from rabota.web import seo as seolib
from tests.web.conftest import NOW
from tests.web.test_seo import Page

# ------------------------------------------------------------------ чистые функции

def test_vacancy_value_uses_midpoint_or_single_bound():
    assert salary.vacancy_value(30000, 40000) == 35000
    assert salary.vacancy_value(40000, 30000) == 35000  # границы перепутаны
    assert salary.vacancy_value(30000, None) == 30000
    assert salary.vacancy_value(None, 45000) == 45000
    assert salary.vacancy_value(None, None) is None
    assert salary.vacancy_value(0, 0) is None


def test_percentile_and_median_match_known_values():
    assert salary.median([1, 2, 3, 4, 5]) == 3
    assert salary.median([1, 2, 3, 4]) == 2.5
    assert salary.median([7]) == 7
    data = sorted([10, 20, 30, 40, 50])
    assert salary.percentile(data, 0.25) == 20 and salary.percentile(data, 0.75) == 40
    assert salary.percentile(data, 0) == 10 and salary.percentile(data, 1) == 50
    with pytest.raises(ValueError):
        salary.percentile([], 0.5)


def test_trim_outliers_removes_far_values_only():
    base = [30000, 31000, 32000, 33000, 34000, 35000]
    assert salary.trim_outliers([*base, 900000]) == base  # одна огромная цифра
    assert salary.trim_outliers([*base, 100]) == base  # и слишком маленькая
    assert salary.trim_outliers(base) == base  # честный разброс остаётся
    assert salary.trim_outliers([1, 2, 1000]) == [1, 2, 1000]  # меньше четырёх значений не трогаем


def test_compute_requires_minimum_sample_after_cleaning():
    four = [30000, 32000, 34000, 36000]
    assert salary.compute("месяц", four) is None
    five = [*four, 38000]
    stat = salary.compute("месяц", five)
    assert stat and stat.n == 5 and stat.median == 34000 and stat.low == 32000 and stat.high == 36000
    assert stat.minimum == 30000 and stat.maximum == 38000 and stat.dropped == 0
    # шесть значений, одно выброс: после очистки пять, порог проходим
    assert salary.compute("месяц", [*five, 2_000_000]).n == 5
    # шесть, два выброса: после очистки четыре, порога нет
    assert salary.compute("месяц", [*four, 2_000_000, 1_500_000]) is None


def test_compute_drops_values_outside_sane_bounds():
    stat = salary.compute("месяц", [30000, 31000, 32000, 33000, 34000, 100, 50_000_000])
    assert stat.n == 5 and stat.dropped == 2
    assert salary.compute("смена", [100, 150, 180, 190, 199]) is None  # всё меньше разумной границы смены


def test_compute_rounds_to_step():
    stat = salary.compute("месяц", [30010, 30120, 30230, 30340, 30450])
    assert stat.median % 100 == 0
    shift = salary.compute("смена", [2510, 2530, 2570, 2610, 2640])
    assert shift.median % 50 == 0


def test_best_prefers_larger_sample_then_month():
    month = salary.compute("месяц", [30000, 31000, 32000, 33000, 34000])
    shift = salary.compute("смена", [2000, 2100, 2200, 2300, 2400, 2500])
    assert salary.best([month, shift]) is shift
    shift5 = salary.compute("смена", [2000, 2100, 2200, 2300, 2400])
    assert salary.best([shift5, month]) is month  # при равенстве месячная
    assert salary.best([]) is None


def test_stat_texts():
    stat = salary.compute("месяц", [30000, 31000, 32000, 33000, 34000])
    assert stat.period_text == "в месяц" and "от" in stat.range_text and "₽" in stat.median_text
    same = salary.compute("месяц", [30000] * 5)
    assert same.range_text == "30\xa0000 ₽"


def test_titles_and_descriptions_fit_search_snippets():
    stat = salary.compute("месяц", [30000, 31000, 32000, 33000, 34000])
    today = datetime(2026, 11, 2)
    for name in ("бариста", "продавец-консультант", "менеджер по работе с клиентами"):
        title = seolib.compose_title(salary.title_for(name, stat))
        assert 10 <= len(title) <= seolib.TITLE_LIMIT + 8, title
        desc = seolib.fit_description(salary.description_for(name, stat, today))
        assert seolib.DESC_MIN <= len(desc) <= seolib.DESC_MAX, desc
        assert "\u2014" not in title + desc
    assert "средняя зарплата" in salary.description_for("бариста", stat, today).lower()
    assert salary.title_for("бариста", None).endswith("в Донецке")


def test_faq_is_made_only_from_data():
    assert salary.faq_for("бариста", [], datetime(2026, 11, 2)) == []
    stat = salary.compute("месяц", [30000, 31000, 32000, 33000, 34000])
    faq = salary.faq_for("бариста", [stat], datetime(2026, 11, 2))
    assert len(faq) == 3 and "32\xa0000" in faq[0][1] and "02.11.2026" in faq[0][1]


# ------------------------------------------------------------------ страницы

def fill(add, n, profession="продавец", base=30000, **kw):
    for i in range(n):
        add(profession=profession, category="торговля", pay_min=base + i * 1000, pay_max=base + 5000 + i * 1000,
            pay_period="месяц", ago=10 + i, **kw)


def test_profession_page_shows_salary_block_from_five_vacancies(client, add):
    fill(add, 4)
    html = client.get("/profession/" + quote("продавец")).text
    assert 'class="sal"' not in html and "Зарплата: Продавец" not in html
    fill(add, 1, base=34000)
    html = client.get("/profession/" + quote("продавец")).text
    assert "Зарплата: Продавец в Донецке" in html and "Медиана по 5&nbsp;" in html and "Обновлено 29.10.2026" in html
    assert 'href="/zarplaty/' + quote("продавец", safe="") + '"' in html


def test_hidden_closed_suspicious_and_pending_are_not_counted(client, add, conn):
    fill(add, 5)
    for kw in ({"status": "closed"}, {"status": "hidden"}, {"suspicious": True}):
        add(profession="продавец", pay_min=500000, pay_max=600000, pay_period="месяц", **kw)
    pid = add(profession="продавец", pay_min=500000, pay_max=600000, pay_period="месяц")
    conn.execute("UPDATE vacancies SET moderation_status='pending' WHERE id=?", (pid,))
    conn.commit()
    add(profession="продавец", pay_min=400000, pay_max=500000, pay_period="месяц", duplicate_of=1)
    stats = salary.stats_for(conn, "продавец")
    assert len(stats) == 1 and stats[0].n == 5 and stats[0].maximum < 100000


def test_vacancies_without_pay_or_other_period_do_not_count(client, add, conn):
    fill(add, 4)
    add(profession="продавец", category="торговля")  # без оплаты
    add(profession="продавец", category="торговля", pay_min=300, pay_period="час")  # час не считаем
    assert salary.stats_for(conn, "продавец") == []


def test_month_and_shift_are_separate_blocks(client, add):
    fill(add, 5)
    for i in range(5):
        add(profession="продавец", category="торговля", pay_min=2000 + i * 100, pay_period="смена", ago=30 + i)
    html = client.get("/zarplaty/" + quote("продавец")).text
    assert "в месяц" in html and "за смену" in html
    assert html.count('class="sal__item"') == 2


def test_salary_page_ssr_json_ld_and_noindex_rules(client, add):
    fill(add, 5)
    r = client.get("/zarplaty/" + quote("Продавец"))
    assert r.status_code == 200 and "noindex" not in r.headers.get("x-robots-tag", "")
    page = Page(r.text)
    assert page.h1 == ["Зарплата: Продавец в Донецке"]
    assert page.links["canonical"] == "https://example.test/zarplaty/" + quote("продавец")
    types = [json.loads(x)["@type"] for x in page.ld]
    assert types == ["Article", "FAQPage", "BreadcrumbList"]
    article = json.loads(page.ld[0])
    assert article["author"]["@type"] == "Organization" and article["dateModified"] == NOW.date().isoformat()
    assert 'class="faq__item"' in r.text  # вопросы и ответы видны на странице, а не только в разметке
    assert "32\xa0500" in r.text or "33\xa0000" in r.text  # медиана посчитана и показана в HTML


def test_salary_page_below_threshold_is_noindex_with_empty_state(client, add):
    fill(add, 4)
    r = client.get("/zarplaty/" + quote("продавец"))
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]
    assert "Пока мало данных" in r.text and page_ld(r) == []


def page_ld(r) -> list[str]:
    return Page(r.text).ld


def test_unknown_profession_is_404(client, add):
    fill(add, 5)
    assert client.get("/zarplaty/" + quote("космонавт")).status_code == 404


def test_salary_index_lists_only_professions_with_enough_data(client, add):
    fill(add, 5)
    fill(add, 3, profession="кассир")
    html = client.get("/zarplaty").text
    assert "/zarplaty/" + quote("продавец", safe="") in html
    assert "/zarplaty/" + quote("кассир", safe="") not in html
    # профессий меньше трёх: общая страница закрыта, пока витрина тонкая
    assert "noindex" in client.get("/zarplaty").headers["x-robots-tag"]


def test_salary_index_indexable_with_three_professions(client, add):
    for prof in ("продавец", "повар", "водитель"):
        fill(add, 5, profession=prof)
    r = client.get("/zarplaty")
    assert "x-robots-tag" not in r.headers
    xml = client.get("/sitemap.xml").text
    assert "https://example.test/zarplaty</loc>" in xml
    for prof in ("продавец", "повар", "водитель"):
        assert "/zarplaty/" + quote(prof) in xml


def test_thin_salary_pages_not_in_sitemap(client, add):
    fill(add, 4)
    fill(add, 5, profession="повар")
    xml = client.get("/sitemap.xml").text
    assert "/zarplaty/" + quote("повар") in xml and "/zarplaty/" + quote("продавец") not in xml


def test_salary_range_bar_has_accessible_label(client, add):
    fill(add, 5)
    html = client.get("/zarplaty/" + quote("продавец")).text
    assert 'role="img"' in html and "Разброс оплаты в месяц" in html
    assert "style=" not in html.split("<main", 1)[1]  # встроенные стили закрыты политикой безопасности
