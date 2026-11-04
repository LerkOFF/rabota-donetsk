"""Кнопка «Редактировать вакансию» на публичной странице: только для администратора, ведёт в форму правки в админке."""
import pytest
from starlette.testclient import TestClient

from tests.accounts.harness import PASSWORD, login, make_user
from tests.web.admin_harness import add_vacancy, login_admin, make_env


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def test_guest_does_not_see_edit_button(env):
    vid = add_vacancy(env)
    with TestClient(env.app, base_url="http://testserver") as guest:
        html = guest.get(f"/vacancy/{vid}").text
    assert "data-admin-edit" not in html and "/admin/vacancies/" not in html


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_other_roles_do_not_see_edit_button(env, role):
    vid = add_vacancy(env)
    make_user(env, f"{role}@example.test", role=role)
    assert login(env, f"{role}@example.test", PASSWORD).status_code == 303
    html = env.client.get(f"/vacancy/{vid}").text
    assert "data-admin-edit" not in html and "/admin/vacancies/" not in html


def test_unverified_admin_does_not_see_edit_button(env):
    vid = add_vacancy(env)
    uid = make_user(env, "boss2@example.test", verified=False)
    conn = env.db()
    try:
        conn.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    login(env, "boss2@example.test", PASSWORD)
    assert "data-admin-edit" not in env.client.get(f"/vacancy/{vid}").text


def test_admin_sees_edit_button_and_it_opens_the_form(env):
    vid = add_vacancy(env)
    login_admin(env)
    html = env.client.get(f"/vacancy/{vid}").text
    href = f"/admin/vacancies/{vid}?edit=1#h-edit"
    assert f'href="{href}"' in html and 'aria-label="Редактировать вакансию"' in html and "admin-fab" in html
    card = env.client.get(f"/admin/vacancies/{vid}?edit=1").text
    assert '<details class="adm-details" open>' in card and f'action="/admin/vacancies/{vid}/edit"' in card
    assert '<details class="adm-details" open>' not in env.client.get(f"/admin/vacancies/{vid}").text
