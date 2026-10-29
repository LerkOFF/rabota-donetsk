"""SEO всех страниц сайта: обход всех маршрутов приложения и проверка заголовков, описаний, canonical, robots,
Open Graph и разметки schema.org.

Правило для новых страниц: маршрут GET, которого нет в таблицах URLS и POLICY ниже, валит тест. Добавьте для него
пример адреса и ожидаемую политику (index или noindex), а в шаблон передайте словарь seo (см. docs/SEO.md).
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from rabota import config, legal
from rabota.db import save_vacancy
from rabota.web import seo
from rabota.web.app import create_app
from rabota.web.queries import open_conn
from tests.accounts.harness import build_env, login, make_user
from tests.cabinet.conftest import make_row
from tests.web.conftest import NOW, make_vacancy

SITE = "https://example.test"
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# ------------------------------------------------------------------ таблицы: адреса маршрутов и ожидаемая политика

# шаблон маршрута -> примеры адресов. Маршрут без параметров проверяется как есть.
URLS: dict[str, list[str]] = {
    "/profession/{name}": ["/profession/" + quote("бариста"), "/profession/" + quote("астроном")],
    "/district/{name}": ["/district/" + quote("Ворошиловский"), "/district/" + quote("Текстильщик")],
    "/city/{name}": ["/city/" + quote("Донецк")],
    "/vacancy/{vacancy_id}": ["/vacancy/{open}", "/vacancy/{closed}", "/vacancy/{pending}"],
    "/company/{company_id}": ["/company/1", "/company/2", "/company/777"],
    "/legal/{slug}": [f"/legal/{d.slug}" for d in legal.list_documents()],
    "/verify/{token}": ["/verify/not-a-real-token"],
    "/reset/{token}": ["/reset/not-a-real-token"],
    "/email/confirm/{token}": ["/email/confirm/not-a-real-token"],
    "/admin/complaints/{complaint_id}": ["/admin/complaints/1"],
    "/admin/requests/{request_id}": ["/admin/requests/1"],
    "/admin/vacancies/{vacancy_id}": ["/admin/vacancies/{open}"],
    "/messages/{thread}": ["/messages/1"],
    "/responses/{application_id}": ["/responses/1"],
    "/notifications/unsubscribe/{token}": ["/notifications/unsubscribe/not-a-real-token"],
    "/vacancy/{vacancy_id}/apply": ["/vacancy/{open}/apply"],
    "/cabinet/vacancies/{vacancy_id}/edit": ["/cabinet/vacancies/{owned}/edit"],
    "/cabinet/vacancies/{vacancy_id}/delete": ["/cabinet/vacancies/{owned}/delete"],
}
# дополнительные адреса к маршрутам без параметров (фильтры, страницы)
EXTRA = {
    "/vacancies": ["/vacancies?daily=1", "/vacancies?q=" + quote("бариста"),
                   "/vacancies?sort=pay", "/vacancies?city=" + quote("Донецк"), "/vacancies?q=" + quote("яяяя")],
}
# маршруты не для поисковиков и не HTML: пропускаются
NOT_HTML_PREFIXES = ("/robots.txt", "/sitemap", "/favicon.ico", "/healthz")


def is_not_html(template: str) -> bool:
    return template.startswith(NOT_HTML_PREFIXES)


# адрес -> index или noindex. Адреса, которых здесь нет, валят тест.
POLICY: dict[str, str] = {
    "/": "index", "/vacancies": "index",
    "/vacancies?daily=1": "noindex", "/vacancies?q=" + quote("бариста"): "noindex", "/vacancies?sort=pay": "noindex",
    "/vacancies?city=" + quote("Донецк"): "noindex", "/vacancies?q=" + quote("яяяя"): "noindex",
    "/profession/" + quote("бариста"): "index", "/profession/" + quote("астроном"): "noindex",
    "/district/" + quote("Ворошиловский"): "index", "/district/" + quote("Текстильщик"): "noindex",
    "/city/" + quote("Донецк"): "index",
    "/vacancy/{open}": "index", "/vacancy/{closed}": "noindex", "/vacancy/{pending}": "noindex",
    "/company/1": "index", "/company/2": "noindex", "/company/777": "noindex",
    "/employers": "index", "/about": "index", "/razmestit": "index",
    "/legal": "index", **{f"/legal/{d.slug}": "index" for d in legal.list_documents()},
    "/razmestit/spasibo": "noindex", "/legal/request": "noindex",
    "/admin": "noindex", "/admin/audit": "noindex", "/admin/complaints": "noindex", "/admin/moderation": "noindex",
    "/admin/reports": "noindex", "/admin/requests": "noindex", "/admin/requests.csv": "noindex",
    "/admin/stoplist": "noindex", "/admin/complaints/1": "noindex", "/admin/requests/1": "noindex",
    "/admin/vacancies/{open}": "noindex",
    "/cabinet/messages": "noindex", "/cabinet/responses": "noindex", "/messages": "noindex", "/messages/1": "noindex",
    "/responses": "noindex", "/responses/1": "noindex", "/notifications": "noindex", "/notifications/unsubscribe/not-a-real-token": "noindex",
    "/vacancy/{open}/apply": "noindex",
    "/login": "noindex", "/register": "noindex", "/forgot": "noindex", "/verify/resend": "noindex",
    "/verify/not-a-real-token": "noindex", "/reset/not-a-real-token": "noindex",
    "/email/confirm/not-a-real-token": "noindex",
    "/account": "noindex", "/account/deleted": "noindex",
    "/cabinet": "noindex", "/cabinet/seeker": "noindex", "/cabinet/employer": "noindex",
    "/cabinet/vacancies": "noindex", "/cabinet/company": "noindex",
    "/cabinet/vacancies/{owned}/edit": "noindex", "/cabinet/vacancies/{owned}/delete": "noindex",
}
# ожидаемая разметка на индексируемых страницах: адрес -> типы JSON-LD в порядке появления
EXPECT_LD: dict[str, list[str]] = {
    "/": ["WebSite", "Organization"],
    "/vacancies": ["ItemList", "BreadcrumbList"],
    "/profession/" + quote("бариста"): ["ItemList", "BreadcrumbList"],
    "/district/" + quote("Ворошиловский"): ["ItemList", "BreadcrumbList"],
    "/city/" + quote("Донецк"): ["ItemList", "BreadcrumbList"],
    "/vacancy/{open}": ["JobPosting", "BreadcrumbList"],
    "/company/1": ["Organization", "BreadcrumbList"],
    "/employers": ["FAQPage"],
    "/about": ["BreadcrumbList"],
    "/legal": ["BreadcrumbList"],
    **{f"/legal/{d.slug}": ["BreadcrumbList"] for d in legal.list_documents()},
}


# ------------------------------------------------------------------ разбор страницы

class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.metas: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.h1: list[str] = []
        self.imgs: list[dict[str, str | None]] = []
        self.ld: list[str] = []
        self._in: str | None = None
        self._buf = ""
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.metas.setdefault(key, a.get("content", ""))
        elif tag == "link" and a.get("rel"):
            self.links.setdefault(a["rel"], a.get("href", ""))
        elif tag == "img":
            self.imgs.append({k: v for k, v in attrs})
        elif tag in ("title", "h1"):
            self._in, self._buf = tag, ""
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in, self._buf = "ld", ""

    def handle_data(self, data: str) -> None:
        if self._in:
            self._buf += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in == "title":
            self.title = self._buf.strip()
        elif tag == "h1" and self._in == "h1":
            self.h1.append(" ".join(self._buf.split()))
        elif tag == "script" and self._in == "ld":
            self.ld.append(self._buf)
        else:
            return
        self._in = None


def ld_types(page: Page) -> list[str]:
    return [json.loads(raw)["@type"] for raw in page.ld]


REQUIRED_LD = {
    "WebSite": ("name", "url", "potentialAction"),
    "Organization": ("name", "url"),
    "BreadcrumbList": ("itemListElement",),
    "ItemList": ("itemListElement",),
    "FAQPage": ("mainEntity",),
    "JobPosting": ("title", "description", "datePosted", "validThrough", "hiringOrganization", "jobLocation",
                   "directApply", "identifier", "url"),
}


def check_ld(page: Page, key: str, policy: str) -> None:
    if policy == "noindex":
        assert page.ld == [], f"{key}: у закрытой страницы не должно быть разметки"
        return
    for raw in page.ld:
        data = json.loads(raw)
        assert data["@context"] == "https://schema.org", key
        kind = data["@type"]
        for field in REQUIRED_LD.get(kind, ()):
            assert data.get(field), f"{key}: у {kind} нет поля {field}"
        if kind == "BreadcrumbList":
            positions = [i["position"] for i in data["itemListElement"]]
            assert positions == list(range(1, len(positions) + 1)), key
            assert all(i["item"].startswith(SITE + "/") and i["name"] for i in data["itemListElement"]), key
        if kind == "ItemList":
            assert all(i["url"].startswith(SITE + "/vacancy/") for i in data["itemListElement"]), key
        if kind == "FAQPage":
            assert all(q["name"] and q["acceptedAnswer"]["text"] for q in data["mainEntity"]), key
        if kind == "JobPosting":
            assert data["hiringOrganization"]["name"], key
            assert data["jobLocation"]["address"]["addressCountry"] == "RU", key
            assert data["validThrough"].endswith("Z") and data["datePosted"] <= data["validThrough"][:10], key
    assert ld_types(page) == EXPECT_LD.get(key, ld_types(page)), key


def check_page(key: str, url: str, resp, policy: str, seen: dict[str, dict[str, str]]) -> None:
    page = Page(resp.text)
    ctx = f"{key} ({url})"
    assert page.lang == "ru", ctx
    assert 10 <= len(page.title) <= 70, f"{ctx}: title {len(page.title)} знаков: {page.title!r}"
    desc = page.metas.get("description", "")
    # у закрытых страниц описание не показывается в выдаче: достаточно осмысленной фразы не длиннее 160 знаков
    low = seo.DESC_MIN if policy == "index" else 40
    assert low <= len(desc) <= seo.DESC_MAX, f"{ctx}: description {len(desc)} знаков: {desc!r}"
    assert "\u2014" not in page.title + desc, ctx
    canonical = page.links.get("canonical", "")
    assert len(page.h1) == 1 and page.h1[0], f"{ctx}: h1 {page.h1}"
    # служебная админка со своей вёрсткой: её не публикуют в соцсетях и не индексируют, ей достаточно закрытия от роботов
    backoffice = urlsplit(url).path.startswith("/admin")
    if not backoffice:
        assert canonical.startswith(SITE + "/"), f"{ctx}: canonical {canonical!r}"
        for prop in ("og:title", "og:description", "og:url", "og:image", "og:type", "og:locale", "og:site_name"):
            assert page.metas.get(prop), f"{ctx}: нет {prop}"
        for name in ("twitter:card", "twitter:title", "twitter:description", "twitter:image"):
            assert page.metas.get(name), f"{ctx}: нет {name}"
        assert page.metas["og:url"] == canonical and page.metas["og:image"].startswith(SITE + "/"), ctx
        assert page.metas.get("theme-color") and page.metas.get("viewport"), ctx
    assert all(img.get("alt") is not None for img in page.imgs), f"{ctx}: картинка без alt"
    robots = page.metas.get("robots", "")
    header = resp.headers.get("x-robots-tag", "")
    if policy == "noindex":
        assert "noindex" in robots and "noindex" in header, f"{ctx}: ждали noindex, meta={robots!r} header={header!r}"
    else:
        assert resp.status_code == 200, ctx
        assert "noindex" not in robots and "noindex" not in header, f"{ctx}: страница закрыта, а должна быть в индексе"
        # canonical на себя: тот же путь, параметр только page
        want = urlsplit(url)
        got = urlsplit(canonical)
        assert unquote(got.path) == unquote(want.path), f"{ctx}: canonical {canonical}"
        assert got.query in ("", ) or re.fullmatch(r"page=\d+", got.query), f"{ctx}: служебные параметры в canonical"
        assert got.query == (want.query if want.query.startswith("page=") else ""), ctx
        assert page.metas["og:title"] == page.title, ctx
        for prev in seen.values():
            assert prev["title"] != page.title, f"{ctx}: заголовок повторяет {prev['url']}"
            assert prev["description"] != desc, f"{ctx}: описание повторяет {prev['url']}"
        seen[key] = {"title": page.title, "description": desc, "url": url}
    check_ld(page, key, policy)


# ------------------------------------------------------------------ данные

@pytest.fixture
def site(tmp_path):
    """Приложение с вакансиями на все случаи, плюс клиенты с настоящим входом по ролям."""
    env = build_env(tmp_path)
    env.client.__enter__()
    conn = open_conn(env.db_path)

    def add(**kw) -> int:
        return save_vacancy(conn, make_vacancy(**kw))

    ids: dict[str, int] = {}
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('c@example.test', '!', 'employer')")
    conn.execute("INSERT INTO companies(owner_user_id, name, description, site, city, verified) "
                 "VALUES (1, 'Кофейня Зерно', 'Сеть кофеен Донецка', 'https://zerno.example', 'Донецк', 1)")
    conn.execute("INSERT INTO companies(owner_user_id, name, city) VALUES (1, 'Пустая компания', 'Донецк')")
    for i in range(4):
        ids["open" if i == 0 else f"b{i}"] = add(
            profession="бариста", district="Ворошиловский", employer="Кофейня Зерно", pay_min=30000 + i * 5000,
            pay_max=45000 + i * 5000, pay_period="месяц", schedule="2/2", employment="постоянно", ago=10 + i,
            summary="Готовить кофе и общаться с гостями в кофейне на проспекте.")
    for i in range(3):
        add(profession="кассир", category="торговля", district="Калининский", pay_min=1800, pay_period="смена",
            ago=20 + i, experience="не нужен")
    add(profession="астроном", category="образование", district="Текстильщик")
    conn.execute("UPDATE vacancies SET origin='direct', company_id=1 WHERE id=?", (ids["open"],))
    ids["closed"] = add(profession="закрытая", status="closed")
    ids["pending"] = add(profession="на проверке")
    conn.execute("UPDATE vacancies SET moderation_status='pending' WHERE id=?", (ids["pending"],))
    add(profession="скрытая", status="hidden")
    add(profession="дубль", duplicate_of=ids["open"])
    conn.commit()
    conn.close()
    env.ids = ids
    clients = {}
    for role, email in (("job_seeker", "anna@example.test"), ("employer", "hr@example.test"), ("admin", "root@example.test")):
        folder = tmp_path / role
        folder.mkdir()
        e = build_env(folder)
        e.client.__enter__()
        uid = make_user(e, email, role="job_seeker" if role == "admin" else role, name="Тест")
        if role == "employer":  # вакансия работодателя с тем же номером, что и открытая в основной базе: страницы правки и удаления
            c3 = e.db()
            make_row(c3, uid, id=ids["open"])
            c3.close()
        if role == "admin":
            c2 = e.db()
            c2.execute("UPDATE users SET role='admin' WHERE email=?", (email,))
            c2.commit()
            c2.close()
        assert login(e, email).status_code == 303
        clients[role] = e
    yield env, clients
    env.client.__exit__(None, None, None)
    for e in clients.values():
        e.client.__exit__(None, None, None)


def resolve(url: str, ids: dict[str, int]) -> str:
    return re.sub(r"\{(open|closed|pending|owned)\}", lambda m: str(ids["open" if m.group(1) == "owned" else m.group(1)]), url)


def walk_routes(routes, prefix: str = ""):
    """Пути всех GET-маршрутов, в том числе из подключённых роутеров (новый FastAPI прячет их во вложенных объектах)."""
    for route in routes:
        if isinstance(route, Mount):
            continue
        inner = getattr(route, "original_router", None) or (getattr(route, "router", None) if hasattr(route, "routes") else None)
        if inner is not None:
            add = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            own = getattr(inner, "prefix", "") or ""
            yield from walk_routes(inner.routes, prefix + add + own)
        elif "GET" in (getattr(route, "methods", None) or ()):
            yield prefix + route.path


def get_routes(app) -> list[str]:
    return sorted(set(walk_routes(app.routes)))


# ------------------------------------------------------------------ обход всех маршрутов

def test_every_get_route_is_covered_and_has_full_seo(site):
    env, clients = site
    seen: dict[str, dict[str, str]] = {}
    checked = 0
    visited: set[str] = set()
    for template in get_routes(env.app):
        if is_not_html(template):
            continue
        if "{" in template:
            assert template in URLS, (
                f"Новый маршрут {template}: добавьте примеры адресов в URLS и политику индексации в POLICY "
                f"(tests/web/test_seo.py), а в шаблон передайте seo (docs/SEO.md)")
            urls = URLS[template]
        else:
            urls = [template, *EXTRA.get(template, [])]
        for key in urls:
            assert key in POLICY, f"Адрес {key}: не указана политика индексации в POLICY (index или noindex)"
            url = resolve(key, env.ids)
            resp = env.client.get(url, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                # вход нужен: закрытый раздел обязан отдать noindex и на перенаправлении, затем смотрим вошедшим
                assert POLICY[key] == "noindex", f"{key}: перенаправление у индексируемой страницы"
                assert "noindex" in resp.headers.get("x-robots-tag", ""), f"{key}: у перенаправления нет X-Robots-Tag"
                for e in clients.values():
                    inner = e.client.get(url, follow_redirects=False)
                    if inner.status_code == 200 and "text/html" in inner.headers["content-type"]:
                        check_page(key, url, inner, "noindex", seen)
                        visited.add(key)
                        checked += 1
                continue
            if "text/html" not in resp.headers.get("content-type", ""):
                continue
            check_page(key, url, resp, POLICY[key], seen)
            visited.add(key)
            checked += 1
    assert checked >= 20, f"проверено только {checked} страниц"
    # закрытые разделы проверены вошедшими пользователями, а не только перенаправлением на вход
    for must in ("/account", "/cabinet/seeker", "/cabinet/employer", "/admin", "/messages", "/responses",
                 "/cabinet/vacancies", "/cabinet/company", "/cabinet/vacancies/{owned}/edit", "/cabinet/vacancies/{owned}/delete"):
        assert must in visited, f"{must}: страница ни разу не открылась с кодом 200, SEO не проверен"


def test_account_pages_checked_for_logged_in_roles(site):
    _, clients = site
    seen: dict[str, dict[str, str]] = {}
    for key in ("/account", "/cabinet/seeker", "/cabinet/employer", "/cabinet/vacancies", "/cabinet/company"):
        pages = [
            e.client.get(key, follow_redirects=False) for e in clients.values()
        ]
        ok = [r for r in pages if r.status_code == 200]
        assert ok, key
        for r in ok:
            check_page(key, key, r, "noindex", seen)


def test_error_pages_have_seo(site):
    env, _ = site
    seen: dict[str, dict[str, str]] = {}
    for key, url, code in (("404", "/net-takoy-stranicy", 404), ("410", f"/vacancy/{env.ids['closed']}", 410)):
        r = env.client.get(url)
        assert r.status_code == code, key
        check_page(key, url, r, "noindex", seen)
    r = env.client.post("/razmestit", data={"profession": "x"}, headers={"origin": "http://testserver"})
    assert r.status_code == 422
    check_page("422", "/razmestit", r, "noindex", seen)
    r = env.client.post("/vacancies")
    assert r.status_code == 405
    check_page("405", "/vacancies", r, "noindex", seen)


# ------------------------------------------------------------------ закрытые вакансии

def test_closed_vacancy_is_410_with_similar_and_noindex(site):
    env, _ = site
    r = env.client.get(f"/vacancy/{env.ids['closed']}")
    assert r.status_code == 410
    assert "Вакансия закрыта" in r.text and "Похожие вакансии" in r.text
    assert "noindex" in r.headers["x-robots-tag"]
    assert "application/ld+json" not in r.text


def test_stale_active_vacancy_is_treated_as_closed(site):
    env, _ = site
    conn = open_conn(env.db_path)
    old = (NOW - timedelta(days=config.VACANCY_TTL_DAYS + 1)).isoformat()
    conn.execute("UPDATE vacancies SET last_seen_at=? WHERE id=?", (old, env.ids["b1"]))
    conn.commit()
    conn.close()
    assert env.client.get(f"/vacancy/{env.ids['b1']}").status_code == 410
    assert env.client.get(f"/vacancy/{env.ids['b2']}").status_code == 200


def test_expired_vacancy_is_410(site):
    env, _ = site
    conn = open_conn(env.db_path)
    conn.execute("UPDATE vacancies SET status='expired' WHERE id=?", (env.ids["b3"],))
    conn.commit()
    conn.close()
    assert env.client.get(f"/vacancy/{env.ids['b3']}").status_code == 410


# ------------------------------------------------------------------ JobPosting

def test_job_posting_fields_and_valid_through_follows_ttl(site):
    env, _ = site
    page = Page(env.client.get(f"/vacancy/{env.ids['open']}").text)
    ld = json.loads(page.ld[0])
    assert ld["@type"] == "JobPosting"
    posted = NOW - timedelta(minutes=10)
    assert ld["datePosted"] == posted.date().isoformat()
    until = posted + timedelta(days=config.VACANCY_TTL_DAYS)
    assert ld["validThrough"] == until.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ld["directApply"] is True
    assert ld["baseSalary"]["value"]["minValue"] == 30000 and ld["baseSalary"]["currency"] == "RUB"
    assert ld["employmentType"] == "FULL_TIME"
    assert "<p>" in ld["description"] and "График: 2/2" in ld["description"]
    assert ld["hiringOrganization"]["sameAs"] == f"{SITE}/company/1"
    crumbs = json.loads(page.ld[1])["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Главная", "Вакансии", "Донецк", "Бариста"]


def test_job_posting_not_direct_apply_for_parsed(site):
    env, _ = site
    ld = json.loads(Page(env.client.get(f"/vacancy/{env.ids['b1']}").text).ld[0])
    assert ld["directApply"] is False


def test_pending_moderation_vacancy_is_noindex(site):
    env, _ = site
    r = env.client.get(f"/vacancy/{env.ids['pending']}")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]


# ------------------------------------------------------------------ страницы-посадки

def test_landing_intro_uses_real_numbers_and_links(site):
    env, _ = site
    html = env.client.get("/profession/" + quote("бариста")).text
    assert "4\xa0актуальные вакансии" in html
    assert "от 30\xa0000 до 60\xa0000 ₽ в месяц" in html
    assert "Ворошиловский район (4)" in html
    assert 'href="/district/' + quote("Ворошиловский", safe="") + '"' in html
    assert 'href="/profession/' + quote("кассир", safe="") + '"' in html  # популярные профессии
    assert 'href="/profession/' + quote("астроном", safe="") + '"' not in html  # тонкая страница не получает ссылок


def test_landing_without_pay_says_so(site):
    env, _ = site
    conn = open_conn(env.db_path)
    for i in range(3):
        save_vacancy(conn, make_vacancy(profession="курьер", category="транспорт", ago=200 + i))
    conn.close()
    html = env.client.get("/profession/" + quote("курьер")).text
    assert "Оплата в этих вакансиях не указана" in html


def test_landing_links_skip_thin_pages(site):
    env, _ = site
    html = env.client.get("/city/" + quote("Донецк")).text
    assert 'href="/district/' + quote("Текстильщик", safe="") + '"' not in html
    assert 'href="/profession/' + quote("астроном", safe="") + '"' not in html
    assert 'href="/district/' + quote("Ворошиловский", safe="") + '"' in html
    assert 'href="/district/' + quote("Калининский", safe="") + '"' in html


def test_thin_listing_is_noindex_and_not_in_sitemap(site):
    env, _ = site
    r = env.client.get("/profession/" + quote("астроном"))
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]
    xml = env.client.get("/sitemap.xml").text
    assert quote("астроном") not in xml and quote("Текстильщик") not in xml
    assert quote("бариста") in xml and quote("Ворошиловский") in xml


def test_listing_pagination_canonical(site):
    env, _ = site
    conn = open_conn(env.db_path)
    for i in range(25):
        save_vacancy(conn, make_vacancy(profession="грузчик", category="склад", ago=100 + i))
    conn.close()
    feed2 = Page(env.client.get("/vacancies?page=2").text)
    assert feed2.links["canonical"] == f"{SITE}/vacancies?page=2" and "robots" not in feed2.metas
    assert feed2.title != Page(env.client.get("/vacancies").text).title
    p1 = Page(env.client.get("/profession/" + quote("грузчик")).text)
    p2 = Page(env.client.get("/profession/" + quote("грузчик") + "?page=2").text)
    assert p1.links["canonical"] == f"{SITE}/profession/" + quote("грузчик")
    assert p2.links["canonical"] == f"{SITE}/profession/" + quote("грузчик") + "?page=2"
    assert p2.title != p1.title and p2.metas["description"] != p1.metas["description"]
    assert p2.links["prev"].endswith(quote("грузчик")) and "next" not in p2.links


def test_profession_case_does_not_create_duplicate(site):
    env, _ = site
    page = Page(env.client.get("/profession/" + quote("Бариста")).text)
    assert page.links["canonical"] == f"{SITE}/profession/" + quote("бариста")


def test_filtered_feed_canonical_goes_to_plain_feed(site):
    env, _ = site
    page = Page(env.client.get("/vacancies?daily=1&sort=pay").text)
    assert page.links["canonical"] == f"{SITE}/vacancies" and "noindex" in page.metas["robots"]


# ------------------------------------------------------------------ карта сайта, robots.txt

def locs(text: str) -> list[str]:
    root = ElementTree.fromstring(text.encode())
    return [e.text for e in root.findall("s:url/s:loc", NS)]


def test_sitemap_has_only_indexable_urls_with_lastmod(site):
    env, _ = site
    r = env.client.get("/sitemap.xml")
    assert r.headers["content-type"].startswith("application/xml")
    urls = locs(r.text)
    ids = env.ids
    assert f"{SITE}/vacancy/{ids['open']}" in urls
    for bad in ("closed", "pending"):
        assert f"{SITE}/vacancy/{ids[bad]}" not in urls
    assert f"{SITE}/company/1" in urls and f"{SITE}/company/2" not in urls
    assert f"{SITE}/profession/" + quote("кассир") in urls
    assert f"{SITE}/city/" + quote("Донецк") in urls
    assert f"{SITE}/legal/privacy" in urls and f"{SITE}/employers" in urls
    root = ElementTree.fromstring(r.content)
    dates = [e.text for e in root.findall("s:url/s:lastmod", NS)]
    assert dates and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates)
    # lastmod ресурса берётся из данных: у бариста последняя публикация 29 октября, не "сегодня"
    for url_el in root.findall("s:url", NS):
        if url_el.find("s:loc", NS).text == f"{SITE}/profession/" + quote("бариста"):
            assert url_el.find("s:lastmod", NS).text == NOW.date().isoformat()


def test_sitemap_splits_into_index_and_typed_files(site, monkeypatch):
    env, _ = site
    monkeypatch.setattr(seo, "SITEMAP_CHUNK", 3)
    r = env.client.get("/sitemap.xml")
    root = ElementTree.fromstring(r.content)
    assert root.tag.endswith("sitemapindex")
    files = [e.text for e in root.findall("s:sitemap/s:loc", NS)]
    assert all(f.startswith(SITE + "/sitemap-") for f in files)
    assert any("sitemap-vacancies-1.xml" in f for f in files) and any("sitemap-professions" in f for f in files)
    assert any("sitemap-companies" in f for f in files) and any("sitemap-static" in f for f in files)
    assert any("sitemap-districts" in f for f in files)
    collected: list[str] = []
    for f in files:
        sub = env.client.get(f.removeprefix(SITE))
        assert sub.status_code == 200 and sub.headers["content-type"].startswith("application/xml"), f
        part = locs(sub.text)
        assert 0 < len(part) <= 3, f
        collected += part
    assert len(collected) == len(set(collected))
    assert f"{SITE}/vacancy/{env.ids['open']}" in collected


def test_sitemap_unknown_file_is_404(site):
    env, _ = site
    assert env.client.get("/sitemap-vacancies-99.xml").status_code == 404
    assert env.client.get("/sitemap-unknown-1.xml").status_code == 404


def test_robots_txt(site):
    env, _ = site
    text = env.client.get("/robots.txt").text
    for line in ("User-agent: *", "Disallow: /login", "Disallow: /register", "Disallow: /account",
                 "Disallow: /cabinet", "Disallow: /admin", "Disallow: /razmestit/spasibo",
                 "Disallow: /vacancy/*/complaint", "Disallow: /vacancy/*/apply",
                 f"Host: {SITE}", f"Sitemap: {SITE}/sitemap.xml"):
        assert line in text, line
    assert "Disallow: /vacancies" not in text and "Disallow: /static" not in text


# ------------------------------------------------------------------ заголовки

def test_noindex_pages_send_x_robots_tag_and_private_paths_always(site):
    env, _ = site
    assert "noindex" in env.client.get("/login").headers["x-robots-tag"]
    assert "noindex" in env.client.get("/cabinet", follow_redirects=False).headers["x-robots-tag"]
    assert "x-robots-tag" not in env.client.get("/about").headers


def test_etag_and_conditional_get_for_public_pages(site):
    env, _ = site
    first = env.client.get("/about")
    tag = first.headers["etag"]
    assert "max-age" in first.headers["cache-control"] and "must-revalidate" in first.headers["cache-control"]
    again = env.client.get("/about", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.headers["etag"] == tag and not again.content
    robots = env.client.get("/robots.txt")
    assert robots.headers["etag"] and "max-age" in robots.headers["cache-control"]
    assert env.client.get("/sitemap.xml").headers["etag"]


def test_no_cache_validators_for_private_or_logged_in(site):
    env, clients = site
    assert "etag" not in env.client.get("/login").headers
    r = clients["job_seeker"].client.get("/about")
    assert "etag" not in r.headers and "no-store" in r.headers["cache-control"]


# ------------------------------------------------------------------ вспомогательные функции seo.py

def test_title_is_trimmed_and_branded():
    assert seo.compose_title("Вход") == "Вход | Работа Донецк"
    assert seo.compose_title("Регистрация: Работа Донецк") == "Регистрация: Работа Донецк"
    long = seo.compose_title("Очень длинное название вакансии для проверки обрезки заголовка по границе слова")
    assert len(long) <= seo.TITLE_LIMIT and long.endswith("…")
    mid = seo.compose_title("Кассир: 12 вакансий в Донецке")
    assert mid.endswith("| Работа Донецк") and len(mid) <= seo.TITLE_LIMIT


def test_description_is_fitted_to_range():
    assert seo.DESC_MIN <= len(seo.fit_description("Коротко.")) <= seo.DESC_MAX
    text = "Первое предложение про работу в Донецке и оплату по договорённости. " * 6
    out = seo.fit_description(text)
    assert seo.DESC_MIN <= len(out) <= seo.DESC_MAX and out.endswith(".")
    words = seo.fit_description("слово " * 80)
    assert len(words) <= seo.DESC_MAX and words.endswith("…")


def test_build_seo_defaults_private_paths_to_noindex():
    data = seo.build_seo(SITE, "/cabinet/messages", title="Сообщения", description="Ваши диалоги")
    assert data["noindex"] and data["jsonld"] == [] and data["canonical"] == SITE + "/cabinet/messages"
    data = seo.build_seo(SITE, "/vacancy/5/apply", title="Отклик")
    assert data["noindex"]


def test_script_json_cannot_close_the_script_tag():
    out = str(seo.script_json({"text": "</script><b>&"}))
    assert "</script>" not in out and "<" not in out and json.loads(out)["text"] == "</script><b>&"


def test_app_without_data_still_has_valid_home(tmp_path):
    app = create_app(tmp_path / "empty.db", site_url=SITE, now=lambda: NOW)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        page = Page(r.text)
        assert ld_types(page) == ["WebSite", "Organization"]
        assert locs(c.get("/sitemap.xml").text)
