"""Профиль вместо кабинета: обзор по ролям на /account, меню без избранного и без пункта «Кабинет», все пункты меню открываются."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from rabota.web.app import ADMIN_MENU, EMPLOYER_MENU, SEEKER_MENU
from tests.accounts.harness import env_impl, login_as
from tests.cabinet.conftest import make_row
from tests.messaging.webkit import apply_as_seeker, make_world, second_visitor

env = pytest.fixture(env_impl)

BANNED = ("избранн", "в разработке")  # резюме теперь есть: docs/RESUMES.md


def visible_text(html: str) -> str:
    body = html.split("<main", 1)[-1]
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", " ", body).lower()


def test_menus_have_no_favorites():
    for menu in (SEEKER_MENU, EMPLOYER_MENU):
        for key, label, href, *_ in menu:
            assert key != "favorites" and "избранн" not in label.lower(), label
            assert href != "/cabinet/favorites"


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_every_menu_item_opens(env, role):
    login_as(env, "u@example.test", role=role, name="Тест")
    menu = SEEKER_MENU if role == "job_seeker" else EMPLOYER_MENU
    for _, label, href, *_ in menu:
        r = env.client.get(href, follow_redirects=False)
        assert r.status_code == 200, f"{label} {href}: {r.status_code}"


def test_menus_have_no_cabinet_item():
    for menu in (SEEKER_MENU, EMPLOYER_MENU, ADMIN_MENU):
        for key, label, href, *_ in menu:
            assert key != "cabinet" and "кабинет" not in label.lower(), label
            assert href not in ("/cabinet", "/cabinet/seeker", "/cabinet/employer"), href
        assert any(href == "/account" for _, _, href, *_ in menu)


def test_employer_profile_overview(env):
    w = make_world(env)
    env.clock.current = datetime.now(UTC)
    c = env.db()
    make_row(c, w.boss_id, profession="повар", status="hidden", moderation="pending", seen=datetime.now(UTC))
    make_row(c, w.boss_id, profession="кассир", status="hidden", moderation="rejected", note="Нет контакта", seen=datetime.now(UTC))
    make_row(c, w.boss_id, profession="грузчик", status="closed", seen=datetime.now(UTC) - timedelta(days=3))
    c.close()
    apply_as_seeker(w)
    html = w.boss.client.get("/account").text
    text = visible_text(html)
    for needle in ("профиль", "опубликовано", "на проверке", "отклонено", "закрыто", "новых откликов", "непрочитанных сообщений",
                   "нет контакта", "разместить вакансию", "мои вакансии", "поиск резюме", "компания", "личные данные"):
        assert needle in text, needle
    for banned in (*BANNED, "кабинет"):
        assert banned not in text, banned
    for href in ("/cabinet/vacancies", "/cabinet/vacancies?state=pending", "/cabinet/company", "/razmestit", "/responses?status=new",
                 "/messages", "/employers/resumes"):
        assert f'href="{href}"' in html, href
    assert html.count("<h1") == 1
    assert 'name="robots" content="noindex' in html


def test_employer_profile_empty_nudges_to_post(env):
    login_as(env, "boss@example.test", role="employer", name="Олег")
    html = env.client.get("/account").text
    assert "Разместите первую вакансию" in html and 'href="/razmestit"' in html
    assert "Заполните карточку компании" in html and 'href="/cabinet/company"' in html


def test_seeker_profile_overview(env):
    w = make_world(env)
    apply_as_seeker(w)
    html = w.seeker.client.get("/account").text
    text = visible_text(html)
    for needle in ("профиль", "мои отклики", "непрочитанных сообщений", "найти вакансию", "составить резюме", "уведомления", "личные данные"):
        assert needle in text, needle
    for banned in (*BANNED, "кабинет"):
        assert banned not in text, banned
    for href in ("/vacancies?direct=1", "/vacancies", "/responses", "/messages", "/notifications", "/alerts", "/cabinet/resumes/new"):
        assert f'href="{href}"' in html, href
    assert html.count("<h1") == 1
    assert 'href="/cabinet/seeker"' not in html


def test_profile_overview_is_mobile_first_and_uses_tokens_only():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "rabota" / "web"
    css = (root / "static" / "auth.css").read_text(encoding="utf-8")
    block = [line for line in css.splitlines() if "acct__overview" in line]
    assert block and not any("#" in line or "rgb" in line or "px" in line for line in block)
    part = (root / "templates" / "auth" / "_overview.html").read_text(encoding="utf-8")
    assert "style=" not in part and "<script" not in part and "\u2014" not in part


def test_dashboards_are_gone_but_old_addresses_redirect(env):
    login_as(env, "s@example.test", role="job_seeker", name="М")
    for path in ("/cabinet", "/cabinet/employer", "/cabinet/seeker"):
        r = env.client.get(path, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/account", path
    other = second_visitor(env)
    login_as(other, "b@example.test", role="employer", name="Б")
    for path in ("/cabinet", "/cabinet/employer", "/cabinet/seeker"):
        assert other.client.get(path, follow_redirects=False).status_code == 301
    assert other.client.get("/cabinet/vacancies").status_code == 200
    assert other.client.get("/cabinet/company").status_code == 200
    assert env.client.get("/cabinet/vacancies").status_code == 403  # чужая роль, как и раньше


@pytest.mark.parametrize("role", ["job_seeker", "employer", "admin"])
def test_no_cabinet_word_in_menu_of_any_role(env, role):
    if role == "admin":
        from rabota.accounts import service

        conn = env.db()
        service.create_admin(conn, "admin@example.test", "admin-pass-phrase-5", env.clock())
        conn.close()
        from tests.accounts.harness import login

        login(env, "admin@example.test", "admin-pass-phrase-5")
    else:
        login_as(env, "u@example.test", role=role, name="Тест")
    html = env.client.get("/account").text
    menu = html.split('class="umenu__panel"', 1)[1].split("</details>", 1)[0]
    assert "кабинет" not in menu.lower() and 'href="/account"' in menu


@pytest.mark.parametrize("path", ["/", "/employers", "/about", "/vacancies", "/register", "/login", "/razmestit"])
def test_public_pages_do_not_promise_favorites(env, path):
    """Резюме на сайте теперь есть, а избранного нет: обещать его на публичных страницах нельзя."""
    text = visible_text(env.client.get(path).text)
    assert "избранн" not in text, path


def test_account_page_and_home_copy(env):
    login_as(env, "s@example.test", role="job_seeker", name="М")
    assert "избранн" not in visible_text(env.client.get("/account").text)
    anon = second_visitor(env)
    home = anon.client.get("/").text
    assert "Отклик на вакансию работодателя в одно нажатие" in home and "Создать аккаунт" in home
    assert "Резюме один раз" not in home
    reg = anon.client.get("/register").text
    assert "Отклики и переписка с работодателями" in reg and "Вакансии, отклики, переписка с соискателями" in reg
