"""Карточка компании в кабинете работодателя и размещение вакансии вошедшим работодателем."""
from __future__ import annotations

import re

import pytest

from tests.accounts.harness import env_impl, login_as
from tests.messaging.webkit import second_visitor, submit

env = pytest.fixture(env_impl)

POST = {
    "profession": "Бариста", "category": "общепит", "city": "Донецк", "summary": "Готовить кофе и общаться с гостями.",
    "phone": "+7 949 000-00-00", "pay_min": "30000", "pay_max": "40000", "pay_period": "месяц", "employer": "Кофейня Зерно",
}


@pytest.fixture
def boss(env):
    uid = login_as(env, "boss@example.test", role="employer", name="Олег Босс")
    return env, uid


def company_rows(env):
    return env.query("SELECT * FROM companies")


# ------------------------------------------------------------------ /cabinet/company

def test_empty_form(boss):
    env, _ = boss
    r = env.client.get("/cabinet/company")
    assert r.status_code == 200
    for name in ("name", "site", "description", "city", "phone", "contact_email"):
        assert f'name="{name}"' in r.text, name
    assert 'name="csrf_token"' in r.text and 'name="robots" content="noindex' in r.text and "no-store" == r.headers["cache-control"]
    assert "<title>Компания" in r.text and r.text.count("<h1") == 1
    assert "Страница компании появится" in r.text


def test_save_creates_company_and_public_page_shows_it(boss):
    env, uid = boss
    r = submit(env, "/cabinet/company", {
        "name": "Кофейня Зерно", "site": "zerno.example", "description": "Сеть кофеен в центре Донецка.",
        "city": "Макеевка", "phone": "8 949 123-45-67", "contact_email": "HR@zerno.example"})
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/company?saved=1"
    rows = company_rows(env)
    assert len(rows) == 1 and rows[0]["owner_user_id"] == uid and rows[0]["site"] == "https://zerno.example"
    assert rows[0]["phone"] == "+79491234567" and rows[0]["contact_email"] == "hr@zerno.example"
    page = env.client.get(f"/company/{rows[0]['id']}").text
    assert "Кофейня Зерно" in page and "Сеть кофеен в центре Донецка." in page and "Макеевка" in page and "https://zerno.example" in page
    assert "+79491234567" not in page and "hr@zerno.example" not in page  # телефон и почта для вакансий не публикуются
    form = env.client.get("/cabinet/company?saved=1").text
    assert "Карточка компании сохранена" in form and 'value="Кофейня Зерно"' in form and f'href="/company/{rows[0]["id"]}"' in form


def test_second_save_updates_the_same_company(boss):
    env, _ = boss
    submit(env, "/cabinet/company", {"name": "Первое"})
    submit(env, "/cabinet/company", {"name": "Второе"})
    rows = company_rows(env)
    assert len(rows) == 1 and rows[0]["name"] == "Второе"


def test_validation_errors(boss):
    env, _ = boss
    r = submit(env, "/cabinet/company", {
        "name": "", "site": "javascript:alert(1)", "phone": "123", "contact_email": "bad", "description": "я" * 2000})
    assert r.status_code == 422
    for needle in ("error-summary", "Укажите название компании", "Адрес сайта в формате", "Телефон в формате", "Проверьте адрес почты", "Описание длиннее"):
        assert needle in r.text, needle
    assert company_rows(env) == []


def test_markup_is_escaped_on_public_page(boss):
    env, _ = boss
    submit(env, "/cabinet/company", {"name": "<b>Зерно</b>", "description": "<script>alert(1)</script> описание"})
    cid = company_rows(env)[0]["id"]
    page = env.client.get(f"/company/{cid}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_seeker_cannot_open(env):
    login_as(env, "s@example.test", role="job_seeker", name="Мария")
    assert env.client.get("/cabinet/company").status_code == 403
    assert env.client.post("/cabinet/company", data={"name": "x"}).status_code == 403


# ------------------------------------------------------------------ /razmestit для работодателя

def test_form_is_prefilled_from_company(boss):
    env, _ = boss
    submit(env, "/cabinet/company", {"name": "Кофейня Зерно", "phone": "+7 949 111-22-33", "contact_email": "hr@zerno.example", "city": "Донецк"})
    html = env.client.get("/razmestit").text
    assert 'value="Кофейня Зерно"' in html and 'value="+79491112233"' in html and 'value="hr@zerno.example"' in html
    assert "в вашем кабинете" in html


def test_guest_form_is_unchanged(env):
    html = env.client.get("/razmestit").text
    assert 'name="website"' in html and "в вашем кабинете" not in html


def test_employer_post_is_bound_to_owner_and_company(boss):
    env, uid = boss
    r = submit(env, "/razmestit", POST)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/vacancies?done=posted"
    v = env.query("SELECT * FROM vacancies")[0]
    assert v["owner_user_id"] == uid and v["origin"] == "direct" and v["moderation_status"] == "pending" and v["status"] == "hidden"
    companies = company_rows(env)
    assert len(companies) == 1 and v["company_id"] == companies[0]["id"] and companies[0]["name"] == "Кофейня Зерно"
    listing = env.client.get(r.headers["location"]).text
    assert "Бариста" in listing and "На проверке" in listing and "отправлена на проверку" in listing


def test_second_post_reuses_company(boss):
    env, _ = boss
    submit(env, "/razmestit", POST)
    submit(env, "/razmestit", {**POST, "profession": "Кассир"})
    assert len(company_rows(env)) == 1
    ids = {r["company_id"] for r in env.query("SELECT company_id FROM vacancies")}
    assert len(ids) == 1


def test_employer_post_without_organization_uses_company_name(boss):
    env, _ = boss
    submit(env, "/cabinet/company", {"name": "Кофейня Зерно"})
    submit(env, "/razmestit", {**POST, "employer": ""})
    assert env.query("SELECT employer FROM vacancies")[0]["employer"] == "Кофейня Зерно"


def test_employer_post_errors_stay_on_form(boss):
    env, _ = boss
    r = submit(env, "/razmestit", {**POST, "profession": ""})
    assert r.status_code == 422 and "Укажите профессию" in r.text
    assert env.query("SELECT COUNT(*) AS n FROM vacancies")[0]["n"] == 0


def test_guest_post_stays_anonymous(env):
    from tests.accounts.harness import post
    r = post(env, "/razmestit", POST, page="/login")
    assert r.status_code == 303 and r.headers["location"] == "/razmestit/spasibo"
    v = env.query("SELECT * FROM vacancies")[0]
    assert v["owner_user_id"] is None and v["company_id"] is None and v["moderation_status"] == "pending"
    assert company_rows(env) == []


def test_seeker_post_stays_anonymous(env):
    login_as(env, "s@example.test", role="job_seeker", name="Мария")
    r = submit(env, "/razmestit", POST)
    assert r.headers["location"] == "/razmestit/spasibo"
    assert env.query("SELECT owner_user_id FROM vacancies")[0]["owner_user_id"] is None


def test_other_employer_does_not_see_company(env):
    login_as(env, "a@example.test", role="employer", name="А")
    submit(env, "/cabinet/company", {"name": "Компания А"})
    b = second_visitor(env)
    login_as(b, "b@example.test", role="employer", name="Б")
    assert 'value="Компания А"' not in b.client.get("/cabinet/company").text
    assert re.search(r'name="name"[^>]*value=""|name="name"(?![^>]*value=)', b.client.get("/cabinet/company").text)
