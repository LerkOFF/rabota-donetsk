"""Кабинеты работодателя и соискателя: сводка вместо заглушек, меню без резюме и избранного, все пункты меню открываются."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from rabota.web.app import EMPLOYER_MENU, SEEKER_MENU
from tests.accounts.harness import env_impl, login_as
from tests.cabinet.conftest import make_row
from tests.messaging.webkit import apply_as_seeker, make_world, second_visitor

env = pytest.fixture(env_impl)

BANNED = ("резюме", "избранн", "в разработке")


def visible_text(html: str) -> str:
    body = html.split("<main", 1)[-1]
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", " ", body).lower()


def test_menus_have_no_resume_or_favorites():
    for menu in (SEEKER_MENU, EMPLOYER_MENU):
        for key, label, href, *_ in menu:
            assert key not in ("resume", "favorites") and "резюме" not in label.lower() and "избранн" not in label.lower(), label
            assert href not in ("/cabinet/resume", "/cabinet/favorites")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_every_menu_item_opens(env, role):
    login_as(env, "u@example.test", role=role, name="Тест")
    menu = SEEKER_MENU if role == "job_seeker" else EMPLOYER_MENU
    for _, label, href, *_ in menu:
        r = env.client.get(href, follow_redirects=False)
        assert r.status_code == 200, f"{label} {href}: {r.status_code}"


def test_employer_dashboard_summary(env):
    w = make_world(env)
    env.clock.current = datetime.now(UTC)
    c = env.db()
    make_row(c, w.boss_id, profession="повар", status="hidden", moderation="pending", seen=datetime.now(UTC))
    make_row(c, w.boss_id, profession="кассир", status="hidden", moderation="rejected", note="Нет контакта", seen=datetime.now(UTC))
    make_row(c, w.boss_id, profession="грузчик", status="closed", seen=datetime.now(UTC) - timedelta(days=3))
    c.close()
    apply_as_seeker(w)
    html = w.boss.client.get("/cabinet/employer").text
    text = visible_text(html)
    for needle in ("кабинет работодателя", "опубликовано", "на проверке", "отклонено", "закрыто", "новых откликов", "непрочитанных сообщений",
                   "нет контакта", "разместить вакансию", "мои вакансии"):
        assert needle in text, needle
    for banned in BANNED:
        assert banned not in text, banned
    for href in ("/cabinet/vacancies", "/cabinet/vacancies?state=pending", "/cabinet/company", "/razmestit", "/responses?status=new", "/messages"):
        assert f'href="{href}"' in html, href
    assert 'name="robots" content="noindex' in html


def test_employer_dashboard_empty_nudges_to_post(env):
    login_as(env, "boss@example.test", role="employer", name="Олег")
    html = env.client.get("/cabinet/employer").text
    assert "Разместите первую вакансию" in html and 'href="/razmestit"' in html
    assert "Заполните карточку компании" in html


def test_seeker_dashboard(env):
    w = make_world(env)
    apply_as_seeker(w)
    html = w.seeker.client.get("/cabinet/seeker").text
    text = visible_text(html)
    for needle in ("кабинет соискателя", "мои отклики", "непрочитанных сообщений", "найти вакансию"):
        assert needle in text, needle
    for banned in BANNED:
        assert banned not in text, banned
    for href in ("/vacancies?direct=1", "/responses", "/messages", "/notifications", "/account"):
        assert f'href="{href}"' in html, href


def test_dashboards_for_wrong_roles(env):
    login_as(env, "s@example.test", role="job_seeker", name="М")
    assert env.client.get("/cabinet/employer").status_code == 403
    other = second_visitor(env)
    login_as(other, "b@example.test", role="employer", name="Б")
    assert other.client.get("/cabinet/seeker").status_code == 403


@pytest.mark.parametrize("path", ["/", "/employers", "/about", "/vacancies", "/register", "/login", "/razmestit"])
def test_public_pages_do_not_promise_resumes_or_favorites(env, path):
    text = visible_text(env.client.get(path).text)
    for banned in ("резюме", "избранн"):
        assert banned not in text, f"{path}: {banned}"


def test_account_page_and_home_copy(env):
    login_as(env, "s@example.test", role="job_seeker", name="М")
    assert "резюме" not in visible_text(env.client.get("/account").text)
    anon = second_visitor(env)
    home = anon.client.get("/").text
    assert "Отклик на вакансию работодателя в одно нажатие" in home and "Создать аккаунт" in home
    assert "Резюме один раз" not in home
    reg = anon.client.get("/register").text
    assert "Отклики и переписка с работодателями" in reg and "Вакансии, отклики, переписка с соискателями" in reg
