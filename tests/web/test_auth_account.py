"""Настройки аккаунта: профиль, пароль, смена почты, удаление."""
import pytest

from rabota.accounts import service
from tests.accounts.harness import (
    PASSWORD,
    build_env,
    env_impl,
    link_in,
    login,
    login_as,
    make_user,
    post,
)

env = pytest.fixture(env_impl)

NEW = "brand-new-pass-9"


def test_account_requires_login(env):
    r = env.client.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/account"


def test_account_page_shows_forms(env):
    login_as(env)
    r = env.client.get("/account")
    assert r.status_code == 200
    for needle in ('action="/account/profile"', 'action="/account/password"', 'action="/account/email"',
                   'action="/account/delete"', "ivan@example.test"):
        assert needle in r.text
    assert r.headers["cache-control"] == "no-store"


def test_profile_update(env):
    login_as(env)
    r = post(env, "/account/profile", {"name": "  Пётр   Иванов ", "phone": "8 (949) 123-45-67"}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=profile"
    row = env.user("ivan@example.test")
    assert (row["name"], row["phone"]) == ("Пётр Иванов", "+79491234567")
    page = env.client.get("/account?saved=profile")
    assert "Профиль сохранён" in page.text and 'value="+79491234567"' in page.text


def test_profile_validation(env):
    login_as(env)
    r = post(env, "/account/profile", {"name": " ", "phone": "abc"}, page="/account")
    assert r.status_code == 422 and "Укажите, как к вам обращаться" in r.text and "Проверьте номер" in r.text
    assert env.user("ivan@example.test")["name"] == "Иван"


def test_profile_phone_can_be_cleared(env):
    login_as(env)
    post(env, "/account/profile", {"name": "Иван", "phone": "+7 949 000-00-00"}, page="/account")
    post(env, "/account/profile", {"name": "Иван", "phone": ""}, page="/account")
    assert env.user("ivan@example.test")["phone"] is None


def test_all_account_posts_require_login(env):
    for path in ("/account/profile", "/account/password", "/account/email", "/account/delete"):
        r = env.client.post(path, data={}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


# ------------------------------------------------------------------ пароль

def test_change_password(env):
    login_as(env)
    r = post(env, "/account/password", {"current_password": PASSWORD, "password": NEW, "password2": NEW}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=password"
    assert env.mailer.last().subject.startswith("Пароль")
    env.client.cookies.clear()
    assert login(env, password=PASSWORD).status_code == 401
    assert login(env, password=NEW).status_code == 303


def test_change_password_wrong_current(env):
    login_as(env)
    r = post(env, "/account/password", {"current_password": "wrong-one-111", "password": NEW, "password2": NEW}, page="/account")
    assert r.status_code == 422 and "Текущий пароль указан неверно" in r.text
    assert service.authenticate(env.db(), "ivan@example.test", PASSWORD).status == "ok"


def test_change_password_weak_or_mismatch(env):
    login_as(env)
    weak = post(env, "/account/password", {"current_password": PASSWORD, "password": "12345678", "password2": "12345678"}, page="/account")
    assert weak.status_code == 422 and "простой" in weak.text
    mismatch = post(env, "/account/password", {"current_password": PASSWORD, "password": NEW, "password2": "x"}, page="/account")
    assert mismatch.status_code == 422 and "не совпадают" in mismatch.text


def test_change_password_attempts_are_limited(tmp_path):
    e = build_env(tmp_path, rate_limits={"password_user": (3, 900)})
    with e.client:
        login_as(e)
        codes = [post(e, "/account/password", {"current_password": "bad-pass-1234", "password": NEW, "password2": NEW}, page="/account").status_code for _ in range(5)]
        assert codes == [422, 422, 422, 429, 429]


# ------------------------------------------------------------------ смена почты

def test_change_email_flow(env):
    login_as(env)
    r = post(env, "/account/email", {"new_email": " New@Example.test ", "password": PASSWORD}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=email_sent"
    mail = env.mailer.last("new@example.test")
    assert mail and link_in(mail).startswith("/email/confirm/")
    assert env.user("ivan@example.test")  # пока без изменений
    assert env.user("new@example.test") is None
    page = env.client.get(link_in(mail))
    assert page.status_code == 200 and "Почта изменена" in page.text
    assert env.user("new@example.test") and env.user("ivan@example.test") is None
    # старый адрес предупреждён
    assert any(m.to == "ivan@example.test" and "изменён" in m.subject for m in env.mailer.outbox)
    # сессии закрыты, вход по новой почте
    assert env.client.get("/account", follow_redirects=False).status_code == 303
    assert login(env, "new@example.test").status_code == 303
    assert login(env, "ivan@example.test").status_code == 401


def test_change_email_needs_correct_password(env):
    login_as(env)
    r = post(env, "/account/email", {"new_email": "new@example.test", "password": "wrong-one-111"}, page="/account")
    assert r.status_code == 422 and "Пароль указан неверно" in r.text
    assert env.mailer.outbox == [] or all(m.to != "new@example.test" for m in env.mailer.outbox)


def test_change_email_validation(env):
    login_as(env)
    bad = post(env, "/account/email", {"new_email": "nope", "password": PASSWORD}, page="/account")
    assert bad.status_code == 422
    same = post(env, "/account/email", {"new_email": "IVAN@example.test", "password": PASSWORD}, page="/account")
    assert same.status_code == 422 and "текущая" in same.text


def test_change_email_taken_address_same_answer(env):
    make_user(env, "other@example.test", name="Другой")
    login_as(env)
    env.mailer.outbox.clear()
    taken = post(env, "/account/email", {"new_email": "other@example.test", "password": PASSWORD}, page="/account")
    free = post(env, "/account/email", {"new_email": "free@example.test", "password": PASSWORD}, page="/account")
    assert taken.status_code == free.status_code == 303
    assert taken.headers["location"] == free.headers["location"]
    to_other = env.mailer.last("other@example.test")
    assert to_other and "/email/confirm/" not in to_other.body  # хозяину занятого адреса ссылка не уходит
    assert env.user("other@example.test")["name"] == "Другой"


def test_change_email_link_expires_and_is_single_use(env):
    login_as(env)
    post(env, "/account/email", {"new_email": "new@example.test", "password": PASSWORD}, page="/account")
    path = link_in(env.mailer.last("new@example.test"))
    env.clock.advance(hours=24, seconds=1)
    assert env.client.get(path).status_code == 400
    post(env, "/account/email", {"new_email": "new@example.test", "password": PASSWORD}, page="/account")
    path2 = link_in(env.mailer.last("new@example.test"))
    assert env.client.get(path2).status_code == 200
    assert env.client.get(path2).status_code == 400


def test_change_email_garbage_token(env):
    assert env.client.get("/email/confirm/garbage").status_code == 400


def test_change_email_smtp_failure_is_reported(tmp_path):
    from tests.accounts.harness import MemoryMailer
    mailer = MemoryMailer()
    e = build_env(tmp_path, mailer=mailer)
    with e.client:
        login_as(e)
        mailer.fail = True
        r = post(e, "/account/email", {"new_email": "new@example.test", "password": PASSWORD}, page="/account")
        assert r.status_code == 503 and "Не удалось отправить письмо" in r.text


def test_change_email_limited_per_user(tmp_path):
    e = build_env(tmp_path, rate_limits={"change_email_user": (2, 3600)})
    with e.client:
        login_as(e)
        codes = [post(e, "/account/email", {"new_email": f"n{i}@example.test", "password": PASSWORD}, page="/account").status_code for i in range(4)]
        assert codes == [303, 303, 429, 429]


# ------------------------------------------------------------------ удаление

def test_delete_account_with_password(env):
    uid = login_as(env)
    r = post(env, "/account/delete", {"password": PASSWORD, "confirm": "1"}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account/deleted"
    assert env.user("ivan@example.test") is None
    assert env.query("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", uid)[0]["n"] == 0
    assert "rabota_session" not in env.client.cookies
    page = env.client.get("/account/deleted")
    assert page.status_code == 200 and "Аккаунт удалён" in page.text


def test_delete_account_cascades_related_data(env):
    uid = login_as(env, role="employer")
    conn = env.db()
    conn.execute("INSERT INTO companies(owner_user_id, name) VALUES (?, 'Фирма')", (uid,))
    conn.execute("INSERT INTO vacancies(profession, owner_user_id, origin) VALUES ('повар', ?, 'site')", (uid,))
    conn.commit()
    conn.close()
    post(env, "/account/delete", {"password": PASSWORD, "confirm": "1"}, page="/account")
    assert env.query("SELECT COUNT(*) AS n FROM companies")[0]["n"] == 0
    assert env.query("SELECT COUNT(*) AS n FROM vacancies")[0]["n"] == 0


def test_delete_account_wrong_password_keeps_everything(env):
    login_as(env)
    r = post(env, "/account/delete", {"password": "wrong-one-111", "confirm": "1"}, page="/account")
    assert r.status_code == 422 and "Пароль указан неверно" in r.text
    assert env.user("ivan@example.test")


def test_delete_account_needs_confirmation_checkbox(env):
    login_as(env)
    r = post(env, "/account/delete", {"password": PASSWORD}, page="/account")
    assert r.status_code == 422 and "Отметьте" in r.text
    assert env.user("ivan@example.test")


def test_admin_cannot_delete_via_site(env):
    conn = env.db()
    service.create_admin(conn, "admin@example.test", "admin-pass-phrase-5", env.clock())
    conn.close()
    assert login(env, "admin@example.test", "admin-pass-phrase-5").status_code == 303
    r = post(env, "/account/delete", {"password": "admin-pass-phrase-5", "confirm": "1"}, page="/account")
    assert r.status_code == 403
    assert env.user("admin@example.test")


def test_delete_attempts_are_limited(tmp_path):
    e = build_env(tmp_path, rate_limits={"delete_user": (2, 900)})
    with e.client:
        login_as(e)
        codes = [post(e, "/account/delete", {"password": "bad-pass-1234", "confirm": "1"}, page="/account").status_code for _ in range(4)]
        assert codes == [422, 422, 429, 429]
