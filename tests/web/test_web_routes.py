"""Маршруты сайта: страницы, фильтры, пагинация, поиск, 404, SEO, безопасность."""
import json
import re
from urllib.parse import quote
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from rabota.web.app import create_app
from tests.web.conftest import NOW

ORIGIN = {"Origin": "http://testserver"}


def titles(html: str) -> list[str]:
    return re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html)


def jsonld(html: str) -> dict:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    assert m, "нет JSON-LD"
    return json.loads(m.group(1))


# ------------------------------------------------------------------ главная и лента


def test_home_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Пока нет вакансий" in r.text
    assert 'name="q"' in r.text
    assert 'lang="ru"' in r.text


def test_home_shows_fresh_stats_and_links(client, add):
    add(profession="бариста", pay_daily=True, experience="не нужен", ago=5)
    add(profession="кассир", category="торговля", ago=10)
    r = client.get("/")
    assert titles(r.text) == ["Бариста", "Кассир"]
    assert "/vacancies?daily=1" in r.text
    assert "/profession/%D0%B1%D0%B0%D1%80%D0%B8%D1%81%D1%82%D0%B0" in r.text
    assert "/vacancies?category=%D0%BE%D0%B1%D1%89%D0%B5%D0%BF%D0%B8%D1%82" in r.text


def test_feed_lists_vacancies_with_card_data(client, add):
    add(profession="бариста", pay_min=4000, pay_max=4000, pay_period="смена", district="Ворошиловский", schedule="2/2",
        work_format="точка", pay_daily=True, experience="не нужен", employer="Кофейня «Зерно»", ago=125)
    html = client.get("/vacancies").text
    assert "Бариста" in html
    assert "4 000 ₽" in html and "за смену" in html
    assert "Донецк, Ворошиловский район" in html
    assert "2/2" in html and "В заведении" in html
    assert "Выплата каждый день" in html and "Без опыта" in html
    assert "2 часа назад" in html
    assert 'href="tel:+79490000000"' in html and ">Оригинал<" in html.replace("</svg>", "")
    assert 'href="https://t.me/demo/1"' in html
    assert "Telegram" in html
    assert "Кофейня «Зерно»" in html


def test_feed_hides_military_duplicates_and_others(client, add):
    ok = add(profession="бариста")
    add(profession="военная служба по контракту", kind="military")
    add(profession="репетитор", kind="service")
    add(profession="дубль бариста", duplicate_of=ok)
    add(profession="закрытая вакансия", status="closed")
    add(profession="мнимая", suspicious=True)
    add(profession="скрытая", status="hidden")
    html = client.get("/vacancies").text
    assert titles(html) == ["Бариста"]
    assert "1\u00a0вакансия" in html


@pytest.mark.parametrize(
    ("qs", "expected"),
    [
        ("city=Макеевка", ["Слесарь"]),
        ("district=Ворошиловский", ["Бариста"]),
        ("category=склад", ["Грузчик"]),
        ("work_format=удалённо", ["Оператор"]),
        ("pay_period=день", ["Грузчик"]),
        ("daily=1", ["Бариста", "Грузчик"]),
        ("no_experience=1", ["Грузчик"]),
        ("side_job=1", ["Грузчик"]),
        ("adult=1", ["Бариста", "Грузчик", "Оператор"]),
        ("daily=1&category=общепит", ["Бариста"]),
        ("q=барист", ["Бариста"]),
        ("q=грузчика", ["Грузчик"]),
        ("q=несуществующее", []),
    ],
)
def test_feed_filters(client, add, qs, expected):
    add(profession="бариста", district="Ворошиловский", pay_daily=True, pay_period="смена", age_min=18, ago=10)
    add(profession="грузчик", category="склад", work_format="склад", pay_period="день", pay_daily=True,
        experience="не нужен", employment="на сегодня", ago=20)
    add(profession="слесарь", city="Макеевка", category="производство", age_min=21, ago=30)
    add(profession="оператор", category="офис", work_format="удалённо", pay_period="месяц", ago=40)
    assert titles(client.get(f"/vacancies?{qs}").text) == expected


def test_feed_no_results_state_has_way_out(client, add):
    add(profession="бариста")
    r = client.get("/vacancies?q=яяяя&daily=1")
    assert r.status_code == 200
    assert "Ничего не нашлось" in r.text
    assert "Сбросить фильтры" in r.text
    assert "0\u00a0вакансий" in r.text
    assert 'name="robots"' in r.text and "noindex" in r.text


def test_feed_active_filters_are_chips_with_remove_links(client, add):
    add()
    html = client.get("/vacancies?daily=1&city=Донецк").text
    assert 'aria-label="Убрать: Выплата каждый день"' in html
    assert 'href="/vacancies?city=%D0%94%D0%BE%D0%BD%D0%B5%D1%86%D0%BA"' in html


def test_feed_pagination(client, add):
    for i in range(45):
        add(profession=f"проф{i:02d}", ago=i)
    p1 = client.get("/vacancies").text
    assert len(titles(p1)) == 20 and "Страница 1 из 3" in p1
    assert 'rel="next" href="/vacancies?page=2"' in p1 and 'rel="prev"' not in p1
    assert "Показать ещё" in p1
    p3 = client.get("/vacancies?page=3").text
    assert len(titles(p3)) == 5 and 'rel="next"' not in p3 and 'rel="prev"' in p3
    names = [t for p in (1, 2, 3) for t in titles(client.get(f"/vacancies?page={p}").text)]
    assert len(names) == len(set(names)) == 45
    assert client.get("/vacancies?page=999").status_code == 200
    assert client.get("/vacancies?page=abc").status_code == 200


def test_feed_pagination_keeps_filters(client, add):
    for i in range(25):
        add(profession=f"проф{i:02d}", pay_daily=True, ago=i)
    html = client.get("/vacancies?daily=1").text
    assert 'href="/vacancies?daily=1&amp;page=2"' in html


def test_feed_selected_filter_options(client, add):
    add(profession="а", city="Донецк")
    add(profession="б", city="Макеевка")
    html = client.get("/vacancies?city=Макеевка").text
    assert re.search(r'<input type="radio" name="city" value="Макеевка" checked>', html)
    assert re.search(r'<input type="radio" name="city" value="Донецк">', html)


# ------------------------------------------------------------------ страница вакансии


def full(add, **kw):
    base = {
        "profession": "бухгалтер", "category": "офис", "pay_min": 48500, "pay_max": 56000, "pay_period": "месяц",
        "schedule": "5/2", "work_format": "офис", "employment": "постоянно", "experience": "от 1 года",
        "requirements": "1С", "district": "Куйбышевский", "employer": "ООО «Донбасс-Снаб»",
        "summary": "Первичка и отчётность.",
        "contacts": {"phones": ["+79490000003"], "telegram": "@hr_demo", "email": "hr@demo.example"},
    }
    base.update(kw)
    return add(**base)


def test_vacancy_page_content(client, add):
    vid = full(add)
    r = client.get(f"/vacancy/{vid}")
    assert r.status_code == 200
    html = r.text
    assert "<h1" in html and "Бухгалтер" in html
    assert "от 48 500 до 56 000 ₽" in html and "в месяц" in html
    assert "Куйбышевский район" in html and "5/2" in html and "В офисе" in html and "Постоянная работа" in html
    assert 'href="tel:+79490000003"' in html and "+7 949 000-00-03" in html
    assert 'href="https://t.me/hr_demo"' in html and 'href="mailto:hr@demo.example"' in html
    assert 'href="https://t.me/demo/1"' in html
    assert "Пожаловаться на вакансию" in html and f'action="/vacancy/{vid}/complaint"' in html
    assert "ООО «Донбасс-Снаб»" in html


def test_vacancy_page_shows_pay_estimate_as_calculation(client, add):
    vid = full(add, pay_min=3000, pay_max=3000, pay_period="смена", pay_monthly_estimate=60000)
    html = client.get(f"/vacancy/{vid}").text
    assert "60 000" in html and "расчёт сайта" in html


def test_vacancy_without_contacts_points_to_original(client, add):
    vid = add(contacts={})
    html = client.get(f"/vacancy/{vid}").text
    assert "Контактов в вакансии нет" in html and 'href="tel:' not in html


def test_vacancy_without_pay_says_so(client, add):
    vid = add()
    html = client.get(f"/vacancy/{vid}").text
    assert "Оплата по договорённости" in html


def test_vacancy_similar_block(client, add):
    vid = full(add)
    add(profession="оператор 1с", category="офис")
    add(profession="водитель", category="транспорт")
    html = client.get(f"/vacancy/{vid}").text
    similar_block = html.split('id="similar"', 1)[1].split("</section>", 1)[0]
    assert "Похожие вакансии" in html and "Оператор 1с" in similar_block and "Водитель" not in similar_block


@pytest.mark.parametrize("kw", [
    {"kind": "military"}, {"kind": "service"}, {"status": "hidden"},
])
def test_vacancy_page_404_for_unlisted(client, add, kw):
    assert client.get(f"/vacancy/{add(**kw)}").status_code == 404


def test_vacancy_missing_and_garbage_ids(client):
    assert client.get("/vacancy/99999").status_code == 404
    assert client.get("/vacancy/abc").status_code in (404, 422)


def test_vacancy_closed_is_gone_with_notice_and_noindex(client, add):
    vid = add(status="closed")
    r = client.get(f"/vacancy/{vid}")
    assert r.status_code == 410
    assert "Вакансия закрыта" in r.text and "noindex" in r.text
    assert "application/ld+json" not in r.text


def test_duplicate_redirects_to_main(client, add):
    main = add(profession="бариста")
    dup = add(profession="бариста", duplicate_of=main)
    r = client.get(f"/vacancy/{dup}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/vacancy/{main}"


def test_duplicate_of_unlisted_main_is_404(client, add):
    main = add(status="closed")
    dup = add(duplicate_of=main)
    assert client.get(f"/vacancy/{dup}").status_code == 404


def test_job_posting_json_ld(client, add):
    vid = full(add, employment="подработка", pay_min=4000, pay_max=4000, pay_period="смена")
    ld = jsonld(client.get(f"/vacancy/{vid}").text)
    assert ld["@type"] == "JobPosting" and ld["title"] == "Бухгалтер"
    assert ld["employmentType"] == "PART_TIME"
    assert ld["baseSalary"]["currency"] == "RUB" and ld["baseSalary"]["value"]["value"] == 4000
    assert ld["datePosted"] == "2026-10-29"
    assert ld["jobLocation"]["address"]["addressLocality"] == "Донецк"
    assert ld["hiringOrganization"]["name"] == "ООО «Донбасс-Снаб»"
    assert ld["url"] == f"https://example.test/vacancy/{vid}"


def test_job_posting_salary_range_and_remote(client, add):
    vid = full(add, work_format="удалённо")
    ld = jsonld(client.get(f"/vacancy/{vid}").text)
    v = ld["baseSalary"]["value"]
    assert (v["minValue"], v["maxValue"], v["unitText"]) == (48500, 56000, "MONTH")
    assert ld["jobLocationType"] == "TELECOMMUTE"


def test_json_ld_cannot_break_out_of_script(client, add):
    vid = add(summary="</script><script>alert(1)</script>")
    html = client.get(f"/vacancy/{vid}").text
    assert "</script><script>alert(1)" not in html


def test_meta_and_open_graph(client, add):
    vid = full(add)
    html = client.get(f"/vacancy/{vid}").text
    assert re.search(r"<title>Бухгалтер, Донецк: от", html)
    assert f'<link rel="canonical" href="https://example.test/vacancy/{vid}">' in html
    for prop in ("og:title", "og:description", "og:url", "og:image", "og:type", "og:locale", "og:site_name"):
        assert f'property="{prop}"' in html
    assert 'name="description"' in html and 'name="viewport"' in html


# ------------------------------------------------------------------ жалобы


def complaint_count(conn):
    return conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]


def test_complaint_saved(client, add, conn):
    vid = add()
    r = client.post(f"/vacancy/{vid}/complaint", data={"reason": "Вакансия не актуальна", "comment": "уже закрыли"},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/vacancy/{vid}?complaint=sent"
    row = conn.execute("SELECT * FROM complaints").fetchone()
    assert row["vacancy_id"] == vid and row["status"] == "new"
    assert row["reason"] == "Вакансия не актуальна: уже закрыли"
    page = client.get(f"/vacancy/{vid}?complaint=sent").text
    assert "жалоба принята" in page


def test_complaint_validation(client, add, conn):
    vid = add()
    r = client.post(f"/vacancy/{vid}/complaint", data={"reason": "придуманная"}, headers=ORIGIN)
    assert r.status_code == 422 and "Выберите причину" in r.text
    r = client.post(f"/vacancy/{vid}/complaint", data={"reason": "Другое", "comment": "ок"}, headers=ORIGIN)
    assert r.status_code == 422 and "Опишите" in r.text and "<details class=\"complain\" open>" in r.text
    assert complaint_count(conn) == 0


def test_complaint_comment_is_trimmed(client, add, conn):
    vid = add()
    client.post(f"/vacancy/{vid}/complaint", data={"reason": "Другое", "comment": "я" * 5000}, headers=ORIGIN)
    assert len(conn.execute("SELECT reason FROM complaints").fetchone()[0]) <= len("Другое: ") + 500


def test_complaint_honeypot_is_ignored_silently(client, add, conn):
    vid = add()
    r = client.post(f"/vacancy/{vid}/complaint", data={"reason": "Другое", "comment": "спам спам", "website": "http://x"},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert complaint_count(conn) == 0


def test_complaint_for_missing_vacancy_is_404(client):
    r = client.post("/vacancy/777/complaint", data={"reason": "Другое", "comment": "никого нет"}, headers=ORIGIN)
    assert r.status_code == 404


def test_complaint_rate_limit(db_path, add, conn):
    app = create_app(db_path, site_url="https://example.test", rate_limits={"complaint": (2, 3600)}, now=lambda: NOW)
    vid = add()
    with TestClient(app) as c:
        codes = [c.post(f"/vacancy/{vid}/complaint", data={"reason": "Вакансия не актуальна"}, headers=ORIGIN,
                        follow_redirects=False).status_code for _ in range(4)]
    assert codes == [303, 303, 429, 429]
    assert complaint_count(conn) == 2


def test_post_from_other_site_is_rejected(client, add, conn):
    vid = add()
    r = client.post(f"/vacancy/{vid}/complaint", data={"reason": "Вакансия не актуальна"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and complaint_count(conn) == 0


def test_oversized_form_is_rejected(client, add):
    vid = add()
    r = client.post(f"/vacancy/{vid}/complaint", content=b"a=" + b"x" * 100_000, headers={
        **ORIGIN, "content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


# ------------------------------------------------------------------ SEO-страницы


def test_profession_page(client, add):
    add(profession="бариста")
    add(profession="бариста", ago=100)
    add(profession="кассир", category="торговля")
    r = client.get("/profession/" + quote("Бариста"))
    assert r.status_code == 200
    assert titles(r.text) == ["Бариста", "Бариста"]
    assert "<h1" in r.text and "2\u00a0актуальные вакансии" in r.text
    assert 'name="robots" content="noindex, follow"' in r.text  # меньше трёх вакансий: страница тонкая
    assert 'rel="canonical" href="https://example.test/profession/' in r.text


def test_profession_page_404_when_nothing(client, add):
    add(profession="бариста")
    assert client.get("/profession/" + quote("астронавт")).status_code == 404


def test_district_and_city_pages(client, add):
    add(profession="а", district="Ворошиловский")
    add(profession="б", district="Калининский")
    add(profession="в", city="Макеевка")
    d = client.get("/district/" + quote("Ворошиловский"))
    assert d.status_code == 200 and titles(d.text) == ["А"] and "Ворошиловский район" in d.text
    c = client.get("/city/" + quote("Макеевка"))
    assert c.status_code == 200 and titles(c.text) == ["В"]
    assert client.get("/district/Нигде").status_code == 404


def test_seo_pages_paginate(client, add):
    for i in range(23):
        add(profession="бариста", ago=i)
    r = client.get("/profession/" + quote("бариста"))
    assert len(titles(r.text)) == 20 and 'rel="next"' in r.text
    assert len(titles(client.get("/profession/" + quote("бариста") + "?page=2").text)) == 3


def test_profession_page_hides_military(client, add):
    add(profession="военнослужащий", kind="military")
    assert client.get("/profession/" + quote("военнослужащий")).status_code == 404


# ------------------------------------------------------------------ размещение вакансии

VALID = {
    "profession": "Бариста", "category": "общепит", "work_format": "точка", "employment": "сменная", "schedule": "2/2",
    "pay_min": "3500", "pay_max": "4000", "pay_period": "смена", "pay_daily": "1", "no_experience": "1", "city": "Донецк",
    "district": "Ворошиловский", "age_min": "18", "requirements": "Медкнижка", "employer": "Кофейня",
    "phone": "8 949 000-00-77", "telegram": "@zerno_hr", "email": "hr@zerno.example",
    "summary": "Готовить кофе и общаться с гостями, обучим с нуля.",
}


def test_post_form_renders(client):
    r = client.get("/razmestit")
    assert r.status_code == 200
    for name in ("profession", "category", "pay_min", "pay_period", "phone", "summary", "website"):
        assert f'name="{name}"' in r.text
    # у каждого поля есть подпись
    assert r.text.count("<label") >= 15


def test_post_valid_saves_hidden_for_moderation(client, conn):
    r = client.post("/razmestit", data=VALID, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/razmestit/spasibo"
    row = conn.execute("SELECT * FROM vacancies").fetchone()
    assert row["source_type"] == "site" and row["status"] == "hidden" and row["kind"] == "vacancy"
    assert row["profession"] == "бариста" and row["pay_min"] == 3500 and row["pay_max"] == 4000
    assert row["pay_period"] == "смена" and row["pay_daily"] == 1 and row["experience"] == "не нужен"
    assert json.loads(row["contacts"]) == {"phones": ["+79490000077"], "telegram": "@zerno_hr", "email": "hr@zerno.example"}
    assert row["published_at"] and row["external_id"] and row["dedupe_key"]
    assert client.get("/vacancies").text.count("vcard__link") == 0  # до проверки в ленте нет
    assert "Вакансия отправлена" in client.get("/razmestit/spasibo").text


def test_post_valid_without_moderation_is_published(db_path, conn):
    app = create_app(db_path, site_url="https://example.test", premoderate=False, now=lambda: NOW)
    with TestClient(app) as c:
        r = c.post("/razmestit", data=VALID, headers=ORIGIN, follow_redirects=False)
        vid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/vacancy/{vid}")
        assert page.status_code == 200 and "Бариста" in page.text
        assert "Оригинал вакансии" not in page.text  # у размещённой вакансии оригинала нет
        assert titles(c.get("/vacancies").text) == ["Бариста"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"profession": ""}, "profession"),
        ({"profession": "аб"}, "profession"),
        ({"category": "космос"}, "category"),
        ({"category": ""}, "category"),
        ({"work_format": "на луне"}, "work_format"),
        ({"employment": "вечно"}, "employment"),
        ({"pay_period": "век"}, "pay_period"),
        ({"pay_period": ""}, "pay_period"),
        ({"pay_min": "5000", "pay_max": "4000"}, "pay_max"),
        ({"pay_min": "99999999"}, "pay_min"),
        ({"age_min": "5"}, "age_min"),
        ({"phone": "123"}, "phone"),
        ({"phone": "", "telegram": "", "email": ""}, "phone"),
        ({"telegram": "@а"}, "telegram"),
        ({"email": "не почта"}, "email"),
        ({"summary": "коротко"}, "summary"),
    ],
)
def test_post_validation_errors(client, conn, change, field):
    data = {**VALID, **change}
    r = client.post("/razmestit", data=data, headers=ORIGIN)
    assert r.status_code == 422
    assert f'id="err-{field}"' in r.text and "Нужно поправить" in r.text
    assert f'href="#p-{field}"' in r.text
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0
    # введённое не теряется
    assert 'value="Ворошиловский"' in r.text


def test_post_errors_keep_values_and_escape(client):
    r = client.post("/razmestit", data={**VALID, "profession": '"><script>alert(1)</script>', "summary": "x"}, headers=ORIGIN)
    assert r.status_code == 422
    assert "<script>alert(1)" not in r.text
    assert "&#34;&gt;&lt;script&gt;" in r.text


def test_post_limits_field_length(client, conn):
    data = {**VALID, "profession": "а" * 300, "summary": "я" * 2500, "employer": "ы" * 400}
    r = client.post("/razmestit", data=data, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    row = conn.execute("SELECT * FROM vacancies").fetchone()
    assert len(row["profession"]) == 80 and len(row["summary"]) == 1000 and len(row["employer"]) == 120


def test_post_honeypot(client, conn):
    r = client.post("/razmestit", data={**VALID, "website": "http://spam"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0


def test_post_rate_limit(db_path, conn):
    app = create_app(db_path, site_url="https://example.test", rate_limits={"post": (2, 3600)}, now=lambda: NOW)
    with TestClient(app) as c:
        codes = [c.post("/razmestit", data=VALID, headers=ORIGIN, follow_redirects=False).status_code for _ in range(3)]
    assert codes == [303, 303, 429]
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 2


def test_post_cross_site_rejected(client, conn):
    r = client.post("/razmestit", data=VALID, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0


def test_post_sql_injection_is_stored_as_text(client, conn):
    r = client.post("/razmestit", data={**VALID, "profession": "'); DROP TABLE vacancies;--"}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT profession FROM vacancies").fetchone()[0].startswith("'); drop table")


# ------------------------------------------------------------------ экранирование


def test_html_is_escaped_in_cards_and_page(client, add):
    evil = '<img src=x onerror=alert(1)>'
    vid = add(profession=evil, employer=evil, summary=evil, schedule=evil, district=evil, title_raw=evil,
              contacts={"phones": ["+79490000000"], "telegram": evil})
    for url in ("/", "/vacancies", f"/vacancy/{vid}"):
        html = client.get(url).text
        assert "<img src=x" not in html, url
        assert "&lt;img src=x" in html, url


def test_search_query_is_escaped(client):
    html = client.get("/vacancies?q=" + quote('"><script>alert(1)</script>')).text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html or "&#34;&gt;&lt;script&gt;" in html


def test_source_url_with_javascript_scheme_is_not_a_link(client, add):
    vid = add(source_url="javascript:alert(1)")
    html = client.get(f"/vacancy/{vid}").text
    assert "javascript:alert" not in html


# ------------------------------------------------------------------ остальное


def test_about_and_error_pages(client):
    assert client.get("/about").status_code == 200
    r = client.get("/net-takoy-stranicy")
    assert r.status_code == 404 and "Страницы нет" in r.text and 'name="q"' in r.text and "noindex" in r.text
    assert client.get("/vacancies/extra").status_code == 404


def test_method_not_allowed_is_html(client):
    r = client.post("/vacancies")
    assert r.status_code == 405 and "Не получилось" in r.text


def test_500_page(db_path, monkeypatch):
    app = create_app(db_path, site_url="https://example.test")

    def boom(*a, **k):
        raise RuntimeError("сломалось")

    monkeypatch.setattr("rabota.web.queries.latest", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/")
    assert r.status_code == 500 and "Что-то сломалось" in r.text and "сломалось\"" not in r.text


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    css = client.get("/static/tokens.css")
    assert css.status_code == 200 and "max-age" in css.headers["cache-control"]
    assert client.get("/static/../app.py").status_code == 404
    assert client.get("/healthz").text == "ok"


def test_robots_and_favicon(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Sitemap: https://example.test/sitemap.xml" in r.text
    assert "Disallow: /vacancy/*/complaint" in r.text
    assert client.get("/favicon.ico", follow_redirects=False).status_code == 301


def test_sitemap(client, add):
    vid = add(profession="бариста", district="Ворошиловский")
    for i in range(2):  # раздел попадает в карту сайта, когда в нём три вакансии и больше
        add(profession="бариста", district="Ворошиловский", ago=60 + i)
    hidden = add(profession="военные", kind="military")
    closed = add(profession="закрыта", status="closed")
    dup = add(profession="дубль", duplicate_of=vid)
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(r.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall("s:url/s:loc", ns)]
    assert "https://example.test/" in locs and "https://example.test/vacancies" in locs
    assert "https://example.test/razmestit" in locs and "https://example.test/about" in locs
    assert f"https://example.test/vacancy/{vid}" in locs
    assert "https://example.test/profession/" + quote("бариста") in locs
    assert "https://example.test/district/" + quote("Ворошиловский") in locs
    assert "https://example.test/city/" + quote("Донецк") in locs
    for bad in (hidden, closed, dup):
        assert f"https://example.test/vacancy/{bad}" not in locs
    assert not any("военные" in quote(x, safe="/:") for x in locs)
    assert all(e.text for e in root.findall("s:url/s:lastmod", ns))


def test_home_has_website_json_ld_and_search_box(client):
    ld = jsonld(client.get("/").text)
    assert ld["@type"] == "WebSite" and ld["potentialAction"]["@type"] == "SearchAction"


def test_filtered_feed_is_noindex_and_canonical_to_plain(client, add):
    for i in range(3):
        add(ago=30 + i)
    plain = client.get("/vacancies").text
    assert "noindex" not in plain
    filtered = client.get("/vacancies?daily=1").text
    assert "noindex" in filtered
    assert '<link rel="canonical" href="https://example.test/vacancies">' in filtered


def test_pages_use_site_url_for_canonical(client):
    assert '<link rel="canonical" href="https://example.test/about">' in client.get("/about").text
