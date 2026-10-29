"""Сброс пароля."""
import pytest

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


def ask_reset(env, email="ivan@example.test"):
    return post(env, "/forgot", {"email": email})


def test_forgot_page(env):
    r = env.client.get("/forgot")
    assert r.status_code == 200 and 'name="email"' in r.text and 'name="csrf_token"' in r.text


def test_reset_full_flow(env):
    make_user(env)
    r = ask_reset(env)
    assert r.status_code == 200 and "Проверьте почту" in r.text
    mail = env.mailer.last("ivan@example.test")
    assert link_in(mail).startswith("/reset/")
    path = link_in(mail)
    assert env.client.get(path).status_code == 200
    r = post(env, path, {"password": NEW, "password2": NEW}, page=path)
    assert r.status_code == 200 and "Пароль изменён" in r.text
    assert login(env, password=PASSWORD).status_code == 401
    assert login(env, password=NEW).status_code == 303
    assert any("изменён" in m.subject for m in env.mailer.outbox)  # уведомление о смене


def test_same_response_for_any_email(env):
    make_user(env)
    known = ask_reset(env, "ivan@example.test")
    unknown = ask_reset(env, "ghost@example.test")
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
    assert [m.to for m in env.mailer.outbox] == ["ivan@example.test"]


def test_forgot_bad_email_format(env):
    assert ask_reset(env, "nope").status_code == 422
    assert env.mailer.outbox == []


def test_reset_token_is_single_use(env):
    make_user(env)
    ask_reset(env)
    path = link_in(env.mailer.last())
    assert post(env, path, {"password": NEW, "password2": NEW}, page=path).status_code == 200
    assert env.client.get(path).status_code == 400
    r = post(env, path, {"password": "another-pass-77", "password2": "another-pass-77"}, page="/forgot")
    assert r.status_code == 400
    assert login(env, password=NEW).status_code == 303


def test_reset_token_expires_after_one_hour(env):
    make_user(env)
    ask_reset(env)
    path = link_in(env.mailer.last())
    env.clock.advance(hours=1, seconds=1)
    assert env.client.get(path).status_code == 400
    assert post(env, path, {"password": NEW, "password2": NEW}, page="/forgot").status_code == 400
    assert login(env, password=PASSWORD).status_code == 303


def test_reset_validates_new_password_and_keeps_token(env):
    make_user(env)
    ask_reset(env)
    path = link_in(env.mailer.last())
    weak = post(env, path, {"password": "password1", "password2": "password1"}, page=path)
    assert weak.status_code == 422 and "простой" in weak.text
    mismatch = post(env, path, {"password": NEW, "password2": NEW + "x"}, page=path)
    assert mismatch.status_code == 422 and "не совпадают" in mismatch.text
    same_as_email = post(env, path, {"password": "ivan@example.test", "password2": "ivan@example.test"}, page=path)
    assert same_as_email.status_code == 422
    assert post(env, path, {"password": NEW, "password2": NEW}, page=path).status_code == 200  # токен цел


def test_reset_closes_all_sessions(env):
    login_as(env)
    assert env.client.get("/account").status_code == 200
    ask_reset(env)
    path = link_in(env.mailer.last())
    post(env, path, {"password": NEW, "password2": NEW}, page=path)
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_second_request_invalidates_first_link(env):
    make_user(env)
    ask_reset(env)
    first = link_in(env.mailer.last())
    ask_reset(env)
    second = link_in(env.mailer.last())
    assert first != second
    assert env.client.get(first).status_code == 400
    assert env.client.get(second).status_code == 200


def test_reset_for_blocked_user_sends_nothing(env):
    from rabota.accounts import service
    uid = make_user(env)
    conn = env.db()
    service.set_blocked(conn, uid, True)
    conn.close()
    r = ask_reset(env)
    assert r.status_code == 200 and env.mailer.outbox == []


def test_reset_garbage_token(env):
    assert env.client.get("/reset/garbage").status_code == 400
    r = post(env, "/reset/garbage", {"password": NEW, "password2": NEW}, page="/forgot")
    assert r.status_code == 400


def test_forgot_rate_limit_per_email_is_silent(env):
    make_user(env)
    for _ in range(6):
        assert ask_reset(env).status_code == 200
    assert len(env.mailer.outbox) == 3


def test_forgot_rate_limit_per_ip(tmp_path):
    e = build_env(tmp_path, rate_limits={"forgot_ip": (2, 3600)})
    with e.client:
        codes = [ask_reset(e, f"u{i}@example.test").status_code for i in range(4)]
        assert codes == [200, 200, 429, 429]


def test_token_guessing_is_throttled(tmp_path):
    e = build_env(tmp_path, rate_limits={"token_ip": (3, 3600)})
    with e.client:
        codes = [e.client.get(f"/reset/guess{i}").status_code for i in range(5)]
        assert codes == [400, 400, 400, 429, 429]


def test_smtp_failure_on_forgot_does_not_crash(tmp_path):
    from tests.accounts.harness import MemoryMailer
    e = build_env(tmp_path, mailer=MemoryMailer(fail=True))
    with e.client:
        make_user(e)
        r = ask_reset(e)
        assert r.status_code == 200 and "Проверьте почту" in r.text


def test_reset_link_does_not_log_in_automatically(env):
    make_user(env)
    ask_reset(env)
    path = link_in(env.mailer.last())
    post(env, path, {"password": NEW, "password2": NEW}, page=path)
    assert "rabota_session" not in env.client.cookies
