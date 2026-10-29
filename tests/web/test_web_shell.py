"""Оболочка сайта и публичные страницы биржи: шапка для гостя и ролей, поиск, фильтры, вакансии работодателей."""
import re

import pytest
from fastapi.testclient import TestClient

from rabota.web.app import create_app, current_user, resolve_where
from tests.accounts.harness import build_env, login, make_user
from tests.web.conftest import NOW

ROLE_USERS = {
    "job_seeker": {"email": "anna@example.test", "name": "Анна Петрова"},
    "employer": {"email": "hr@example.test", "name": "Кофейня Зерно"},
    "admin": {"email": "root@example.test", "name": ""},
}


@pytest.fixture
def make_client(tmp_path):
    """make_client(role) -> клиент с настоящим входом: пользователь создан в базе, сессия получена через /login."""
    envs = []

    def _make(role=None, name=None):
        folder = tmp_path / f"env{len(envs)}"
        folder.mkdir()
        e = build_env(folder)
        envs.append(e)
        e.client.__enter__()
        if role:
            u = ROLE_USERS[role]
            nm = u["name"] if name is None else name
            reg_role = "job_seeker" if role == "admin" else role
            make_user(e, u["email"], role=reg_role, name=nm or "x")
            if role == "admin" or not nm:
                conn = e.db()
                conn.execute("UPDATE users SET role = ?, name = ? WHERE email = ?", (role, nm, u["email"]))
                conn.commit()
                conn.close()
            assert login(e, u["email"]).status_code == 303
        return e.client

    yield _make
    for e in envs:
        e.client.__exit__(None, None, None)


def mark_direct(conn, vid, company_id=None):
    conn.execute("UPDATE vacancies SET origin = 'direct', company_id = ? WHERE id = ?", (company_id, vid))
    conn.commit()


def header_of(html: str) -> str:
    return html.split("</header>", 1)[0]


# ------------------------------------------------------------------ шапка


def test_guest_header_has_login_register_and_search(client):
    head = header_of(client.get("/vacancies").text)
    assert 'href="/login"' in head and 'href="/register"' in head
    assert "Войти" in head and "Регистрация" in head
    assert 'href="/vacancies"' in head and 'href="/employers"' in head and "Работодателям" in head
    assert 'name="q"' in head and 'name="where"' in head and 'role="search"' in head
    assert "/logout" not in head and "umenu" not in head
    assert 'data-menu-toggle' in head and 'aria-controls="mmenu"' in head


def test_header_without_state_user_attribute_is_guest(client):
    # middleware аккаунтов не подключён: атрибута request.state.user нет совсем
    assert 'href="/login"' in client.get("/about").text


def test_seeker_header_menu(make_client):
    html = make_client("job_seeker").get("/vacancies").text
    head = header_of(html)
    assert 'href="/login"' not in head and 'href="/register"' not in head
    for label, href in [("Отклики", "/responses"), ("Сообщения", "/messages"), ("Профиль", "/account")]:
        assert f'href="{href}"' in head and label in head
    assert "Резюме" not in head and "Избранное" not in head
    assert "/cabinet/resume" not in head and "/cabinet/favorites" not in head
    assert "Анна Петрова" in head and "anna@example.test" in head
    assert re.search(r'<form class="umenu__out" method="post" action="/logout">', head)
    assert "hpost" not in head
    assert "Мои вакансии" not in head


def test_employer_header_menu_and_post_button(make_client):
    head = header_of(make_client("employer").get("/").text)
    for label, href in [("Мои вакансии", "/cabinet/vacancies"), ("Отклики", "/responses"), ("Сообщения", "/messages"),
                        ("Компания", "/cabinet/company"), ("Профиль", "/account")]:
        assert f'href="{href}"' in head and label in head
    assert 'class="btn btn--primary btn--sm hpost" href="/razmestit"' in head
    assert "Резюме" not in head and "Избранное" not in head
    assert 'action="/logout"' in head


def test_admin_and_nameless_user_header(make_client):
    head = header_of(make_client("admin").get("/").text)
    assert 'href="/cabinet"' in head and 'href="/account"' in head
    assert "root@example.test" in head  # без имени подпись берётся из почты
    assert "hpost" not in head


def test_logout_form_carries_real_csrf_token(make_client):
    html = make_client("employer").get("/").text
    form = html.split('action="/logout"', 1)[1].split("</form>", 1)[0]
    assert re.search(r'name="csrf_token" value="[^"]{16,}"', form)


def test_user_text_is_escaped_in_header(make_client):
    html = make_client("job_seeker", name="<b>Аня</b>").get("/").text
    assert "<b>Аня</b>" not in html and "&lt;b&gt;Аня&lt;/b&gt;" in html


def test_current_user_helper():
    class Req:
        class state:
            pass

    assert current_user(Req) is None
    Req.state.user = {"id": 1, "email": "a@b.c", "name": "Иван", "role": "employer"}
    u = current_user(Req)
    assert u["is_employer"] and u["initial"] == "И" and [m["key"] for m in u["menu"]][:2] == ["cabinet", "vacancies"]
    Req.state.user = {"id": 1, "email": "a@b.c", "name": "", "role": "job_seeker"}
    assert current_user(Req)["label"] == "a@b.c"


def test_header_search_is_hidden_on_home_and_employers_but_hero_has_search(client):
    home = client.get("/").text
    assert header_of(home).count('role="search"') == 0
    assert home.count('role="search"') == 1 and 'id="hero-q"' in home and 'id="hero-where"' in home
    assert client.get("/employers").text.count('role="search"') == 0


def test_search_inputs_have_unique_ids_and_labels(client, add):
    add(profession="бариста")
    for path in ("/", "/vacancies", "/about", "/employers", "/nope"):
        html = client.get(path).text
        ids = re.findall(r' id="([^"]+)"', html)
        assert len(ids) == len(set(ids)), path
        for fid in re.findall(r'<input[^>]*id="([\w-]+)"[^>]*name="(?:q|where)"', html):
            assert f'for="{fid}"' in html


def test_search_datalists_offer_professions_and_places(db_path, add):
    add(profession="бариста", district="Ворошиловский")
    add(profession="кассир", city="Макеевка")
    app = create_app(db_path, site_url="https://example.test", now=lambda: NOW)
    html = TestClient(app).get("/").text
    prof = re.search(r'<datalist id="hero-prof">(.*?)</datalist>', html, re.DOTALL).group(1)
    place = re.search(r'<datalist id="hero-place">(.*?)</datalist>', html, re.DOTALL).group(1)
    assert 'value="Бариста"' in prof and 'value="Кассир"' in prof
    assert 'value="Донецк"' in place and 'value="Макеевка"' in place and 'value="Ворошиловский район"' in place


def test_footer_has_legal_links_sections_and_contacts(client):
    html = client.get("/about").text
    foot = html.split("<footer", 1)[1]
    for href in ("/legal/privacy", "/legal/terms", "/legal/consent", "/razmestit", "/employers", "/about#contacts", "/vacancies?daily=1"):
        assert f'href="{href}"' in foot, href
    for title in ("Соискателям", "Работодателям", "О сайте"):
        assert title in foot
    assert 'id="contacts"' in html


def test_footer_shows_contact_email_when_configured(db_path, monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "help@example.test")
    app = create_app(db_path, site_url="https://example.test", now=lambda: NOW)
    html = TestClient(app).get("/about").text
    assert 'href="mailto:help@example.test"' in html


# ------------------------------------------------------------------ поиск "где"


def facets(**kw):
    base = {"cities": [("Донецк", 5), ("Макеевка", 2)], "districts": [("Ворошиловский", 3), ("Калининский", 1)]}
    base.update(kw)
    return base


@pytest.mark.parametrize(("where", "key", "value"), [
    ("Донецк", "city", "Донецк"),
    ("донецк", "city", "Донецк"),
    ("Ворошиловский район", "district", "Ворошиловский"),
    ("ворошиловский", "district", "Ворошиловский"),
    ("Макеевка", "city", "Макеевка"),
    ("Ясиноватая", "city", "Ясиноватая"),
])
def test_resolve_where(where, key, value):
    out = resolve_where({"where": where, "q": "кассир"}, facets())
    assert out[key] == value and out["q"] == "кассир"


def test_resolve_where_keeps_explicit_filters_and_ignores_empty():
    assert resolve_where({"where": "Донецк", "city": "Макеевка"}, facets())["city"] == "Макеевка"
    assert "city" not in resolve_where({"where": "  "}, facets())
    assert "city" not in resolve_where({}, facets())


def test_feed_where_district_and_city(client, add):
    add(profession="а", district="Ворошиловский")
    add(profession="б", district="Калининский")
    add(profession="в", city="Макеевка")
    html = client.get("/vacancies?where=Ворошиловский район").text
    assert re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html) == ["А"]
    assert 'value="Ворошиловский район"' in header_of(html)  # поле "Где" помнит запрос
    assert "Ворошиловский район" in html.split('aria-label="Включённые фильтры"', 1)[1]
    html = client.get("/vacancies?where=Макеевка").text
    assert re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html) == ["В"]


def test_feed_where_unknown_place_shows_no_results(client, add):
    add(profession="а")
    html = client.get("/vacancies?where=Нигде").text
    assert "Ничего не нашлось" in html and "0\xa0вакансий" in html


def test_feed_where_pagination_keeps_resolved_filter(client, add):
    for i in range(25):
        add(profession=f"п{i}", district="Ворошиловский", ago=i + 1)
    html = client.get("/vacancies?where=Ворошиловский").text
    assert 'href="/vacancies?district=%D0%92%D0%BE%D1%80%D0%BE%D1%88%D0%B8%D0%BB%D0%BE%D0%B2%D1%81%D0%BA%D0%B8%D0%B9&amp;page=2"' in html


# ------------------------------------------------------------------ лента: фильтры, сортировка, отметки


def test_filters_panel_has_all_groups(client, add):
    add(profession="а", district="Ворошиловский", pay_min=1, pay_period="месяц", work_format="офис")
    html = client.get("/vacancies").text
    panel = html.split('id="filters"', 1)[1].split('class="feed__main"', 1)[0]
    for name in ("city", "district", "category", "work_format", "pay_period"):
        assert f'name="{name}"' in panel, name
    for name in ("pay_from", "no_experience", "side_job", "daily", "adult", "direct"):
        assert f'name="{name}"' in panel, name
    assert panel.count("<details") >= 7 and "Только от работодателей" in panel
    assert 'aria-controls="filters"' in html and "data-filters-close" in html and "data-scrim" in html


def test_filters_panel_marks_chosen_values(client, add):
    add(profession="а", district="Ворошиловский")
    html = client.get("/vacancies?district=Ворошиловский&pay_from=30000&direct=1&daily=1").text
    assert re.search(r'name="district" value="Ворошиловский" checked', html)
    assert 'name="pay_from"' in html and 'value="30000"' in html
    assert re.search(r'name="direct" value="1" checked', html) and re.search(r'name="daily" value="1" checked', html)
    assert "Фильтры (4)" in html


def test_feed_pay_from_filters_and_chip(client, add):
    add(profession="дорогая", pay_min=60000, pay_max=60000, pay_period="месяц")
    add(profession="дешёвая", pay_min=20000, pay_max=20000, pay_period="месяц")
    html = client.get("/vacancies?pay_from=30000").text
    assert re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html) == ["Дорогая"]
    assert "Оплата от 30\xa0000 ₽" in html and 'aria-label="Убрать: Оплата от 30' in html
    assert 'href="/vacancies"' in html.split('aria-label="Включённые фильтры"', 1)[1]


def test_feed_pay_from_garbage_is_ignored(client, add):
    add(profession="а")
    html = client.get("/vacancies?pay_from=abc%27%3B--").text
    assert "1\xa0вакансия" in html and "chip--remove" not in html


def test_feed_sort_by_pay_and_select(client, add):
    add(profession="средняя", pay_min=40000, pay_max=40000, pay_period="месяц", ago=1)
    add(profession="высокая", pay_min=90000, pay_max=90000, pay_period="месяц", ago=50)
    titles_ = lambda h: re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', h)
    assert titles_(client.get("/vacancies").text) == ["Средняя", "Высокая"]
    html = client.get("/vacancies?sort=pay").text
    assert titles_(html) == ["Высокая", "Средняя"]
    assert re.search(r'<option value="pay" selected>По оплате</option>', html)
    assert re.search(r'<option value="" selected>По дате</option>', client.get("/vacancies").text)
    assert 'name="sort"' in html and "data-submit-on-change" in html


def test_feed_sort_is_noindex_and_kept_in_pagination(client, add):
    for i in range(22):
        add(profession=f"п{i}", pay_min=1000 + i, pay_period="месяц", ago=i + 1)
    html = client.get("/vacancies?sort=pay").text
    assert 'name="robots" content="noindex, follow"' in html
    assert 'href="/vacancies?sort=pay&amp;page=2"' in html


def test_feed_sort_garbage_falls_back_to_date(client, add):
    add(profession="а")
    r = client.get("/vacancies?sort=%27%20OR%201%3D1")
    assert r.status_code == 200 and "1\xa0вакансия" in r.text


def test_feed_direct_filter_and_chip(client, add, conn):
    a = add(profession="своя")
    add(profession="из канала")
    mark_direct(conn, a)
    html = client.get("/vacancies?direct=1").text
    assert re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html) == ["Своя"]
    assert "От работодателя" in html.split('aria-label="Включённые фильтры"', 1)[1]


def test_card_marks_direct_vacancy_and_offers_apply(client, add, conn):
    direct = add(profession="своя", source_url="")
    parsed = add(profession="чужая")
    mark_direct(conn, direct)
    html = client.get("/vacancies").text
    cards = dict(zip(("a", "b"), re.split(r'(?=<article class="vcard)', html)[1:], strict=True))
    own = next(c for c in cards.values() if "Своя" in c)
    other = next(c for c in cards.values() if "Чужая" in c)
    assert "vcard--direct" in own and "От работодателя" in own
    assert f'href="/vacancy/{direct}/apply"' in own and "Откликнуться" in own and "Оригинал" not in own
    assert "vcard--direct" not in other and "От работодателя" not in other
    assert "Откликнуться" not in other and "Оригинал" in other and "Позвонить" in other
    assert parsed  # парная вакансия осталась обычной


def test_home_counts_direct_and_links_to_it(client, add, conn):
    add(profession="а")
    mark_direct(conn, add(profession="б"))
    html = client.get("/").text
    assert 'href="/vacancies?direct=1"' in html
    assert re.search(r'От работодателей<span class="today__n">1</span>', html)


def test_home_has_all_blocks(client, add):
    add(profession="бариста", category="общепит", ago=5)
    html = client.get("/").text
    for part in ("Сферы работы", "Свежие вакансии", "Ищут чаще всего", "Ищете сотрудника?", "Ищете работу?", "Популярные профессии"):
        assert part in html, part
    assert 'href="/razmestit"' in html and 'href="/register"' in html and 'href="/employers"' in html


def test_home_for_logged_in_user_links_to_cabinet(make_client):
    html = make_client("job_seeker").get("/").text
    assert 'href="/cabinet"' in html and "Создать резюме" not in html.split("Ищете работу?", 1)[1].split("</section>", 1)[0]


# ------------------------------------------------------------------ страница вакансии


def test_parsed_vacancy_sidebar_has_call_and_original(client, add):
    vid = add(profession="бариста", contacts={"phones": ["+79490000001"]})
    html = client.get(f"/vacancy/{vid}").text
    side = html.split('class="vpage__side"', 1)[1].split("</aside>", 1)[0]
    assert "Связаться" in side and 'href="tel:+79490000001"' in side
    assert "Открыть оригинал" in side and "Откликнуться" not in side
    assert "/apply" not in html


def test_direct_vacancy_sidebar_has_apply_button_and_company_card(client, add, conn):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('c@example.test', '!', 'employer')")
    conn.execute("INSERT INTO companies(owner_user_id, name) VALUES (1, 'Кофейня Зерно')")
    conn.commit()
    vid = add(profession="бариста", employer="Кофейня Зерно", source_url="")
    mark_direct(conn, vid, company_id=1)
    html = client.get(f"/vacancy/{vid}").text
    side = html.split('class="vpage__side"', 1)[1].split("</aside>", 1)[0]
    assert f'href="/vacancy/{vid}/apply"' in side and ">Откликнуться<" in re.sub(r"<svg.*?</svg>", "", side, flags=re.DOTALL)
    assert "Открыть оригинал" not in side
    assert 'href="/company/1"' in side and "Работодатель на сайте" in side
    assert "От работодателя" in html.split('class="vpage__side"', 1)[0]
    assert f'class="action-bar"><a class="btn btn--primary" href="/vacancy/{vid}/apply"' in html.replace("\n", "").replace("  ", "")


def test_direct_vacancy_without_contacts_still_has_apply(client, add, conn):
    vid = add(profession="бариста", contacts={}, source_url="")
    mark_direct(conn, vid)
    html = client.get(f"/vacancy/{vid}").text
    assert "Работодатель принимает отклики на сайте" in html and f"/vacancy/{vid}/apply" in html


def test_vacancy_page_has_employer_card_for_parsed(client, add):
    vid = add(profession="бариста", employer="Кофейня «Зерно»")
    html = client.get(f"/vacancy/{vid}").text
    assert "Источник: Telegram" in html and "О компании и её вакансии" not in html


def test_apply_path_is_closed_for_robots(client):
    assert "Disallow: /vacancy/*/apply" in client.get("/robots.txt").text


# ------------------------------------------------------------------ работодатели


def test_employers_landing(client, add):
    add(profession="а", ago=10)
    r = client.get("/employers")
    assert r.status_code == 200
    html = r.text
    assert html.count("<h1") == 1 and "Найдите сотрудника в Донецке" in html
    assert 'href="/razmestit"' in html and 'href="/register"' in html
    for part in ("Что вы получаете", "Как разместить вакансию", "Частые вопросы"):
        assert part in html
    assert html.count("<details") == 4 and "1\xa0вакансия" in html
    assert 'rel="canonical" href="https://example.test/employers"' in html


def test_employers_landing_for_logged_in_employer(make_client):
    html = make_client("employer").get("/employers").text
    assert 'href="/cabinet"' in html and "Создать кабинет" not in html


def test_company_page_stub_for_unknown_id(client):
    r = client.get("/company/12345")
    assert r.status_code == 200
    html = r.text
    assert "Название компании" in html and "Профиль компании ещё оформляется" in html
    assert "Открытых вакансий нет" in html
    assert 'name="robots" content="noindex, follow"' in html


def test_company_page_with_data_and_vacancies(client, add, conn):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('c@example.test', '!', 'employer')")
    conn.execute("INSERT INTO companies(owner_user_id, name, description, site, city, verified) "
                 "VALUES (1, 'Кофейня Зерно', 'Сеть кофеен <b>Донецка</b>', 'https://zerno.example', 'Донецк', 1)")
    conn.commit()
    mine = add(profession="бариста")
    mark_direct(conn, mine, company_id=1)
    add(profession="чужая")
    html = client.get("/company/1").text
    assert "Кофейня Зерно" in html and "Проверенный работодатель" in html
    assert "&lt;b&gt;Донецка&lt;/b&gt;" in html and "<b>Донецка</b>" not in html
    assert re.findall(r'class="vcard__link" href="/vacancy/\d+">([^<]+)<', html) == ["Бариста"]
    assert 'href="https://zerno.example"' in html and "Профиль компании ещё оформляется" not in html
    assert 'name="robots"' not in html


def test_company_site_with_bad_scheme_is_not_a_link(client, conn):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('c@example.test', '!', 'employer')")
    conn.execute("INSERT INTO companies(owner_user_id, name, site) VALUES (1, 'Х', 'javascript:alert(1)')")
    conn.commit()
    assert 'href="javascript:' not in client.get("/company/1").text


def test_company_bad_id_is_404(client):
    assert client.get("/company/abc").status_code in (404, 422)


def test_sitemap_has_employers(client):
    assert "https://example.test/employers" in client.get("/sitemap.xml").text


def test_sitemap_has_legal_documents(client):
    xml = client.get("/sitemap.xml").text
    for slug in ("", "/privacy", "/terms", "/consent", "/cookies"):
        assert f"https://example.test/legal{slug}</loc>" in xml


def test_footer_links_to_cookies_and_register_consent_links_to_document(client):
    assert 'href="/legal/cookies"' in client.get("/about").text
    assert 'href="/legal/consent"' in client.get("/register").text
