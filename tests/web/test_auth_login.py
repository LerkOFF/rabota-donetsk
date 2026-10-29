"""Вход, выход, сессии."""
import re
from datetime import timedelta

import pytest

from tests.accounts.harness import (
    PASSWORD,
    build_env,
    csrf_of,
    env_impl,
    link_in,
    login,
    login_as,
    make_user,
    post,
    register,
)

env = pytest.fixture(env_impl)


def session_cookie_header(resp) -> str:
    for h in resp.headers.get_list("set-cookie"):
        if h.startswith("rabota_session="):
            return h
    return ""


def test_login_page(env):
    r = env.client.get("/login")
    assert r.status_code == 200
    assert 'name="email"' in r.text and 'name="password"' in r.text and 'name="csrf_token"' in r.text
    assert 'autocomplete="current-password"' in r.text
    assert "/forgot" in r.text and "/register" in r.text


def test_login_before_verification_is_closed_with_resend_button(env):
    register(env)
    r = login(env)
    assert r.status_code == 403
    assert "Почта ещё не подтверждена" in r.text
    assert 'action="/verify/resend"' in r.text and "Отправить письмо ещё раз" in r.text
    assert session_cookie_header(r) == ""


def test_login_after_verification(env):
    register(env)
    env.client.get(link_in(env.mailer.last()))
    r = login(env)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"
    assert session_cookie_header(r)
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1
    assert env.user("ivan@example.test")["last_login_at"]


def test_wrong_password_and_unknown_email_look_the_same(env):
    make_user(env)
    a = login(env, password="wrong-password-1")
    b = login(env, email="nobody@example.test", password="wrong-password-1")
    assert a.status_code == b.status_code == 401
    assert "Неверная почта или пароль" in a.text
    strip = lambda t: re.sub(r'value="[^"]*"|name="csrf_token" value="[^"]*"', "", t)
    assert strip(a.text) == strip(b.text)
    assert session_cookie_header(a) == "" and session_cookie_header(b) == ""


def test_wrong_password_does_not_echo_password(env):
    make_user(env)
    r = login(env, password="wrong-password-1")
    assert "wrong-password-1" not in r.text


def test_email_case_and_spaces_are_normalized_on_login(env):
    make_user(env)
    assert login(env, email="  IVAN@Example.test ").status_code == 303


def test_blocked_user_cannot_login(env):
    uid = make_user(env)
    conn = env.db()
    from rabota.accounts import service
    service.set_blocked(conn, uid, True)
    conn.close()
    r = login(env)
    assert r.status_code == 403 and "заблокирован" in r.text and session_cookie_header(r) == ""
    # без верного пароля блокировку не раскрываем
    assert "заблокирован" not in login(env, password="wrong-password-1").text


def test_blocking_kills_existing_session(env):
    uid = login_as(env)
    assert env.client.get("/account").status_code == 200
    from rabota.accounts import service
    conn = env.db()
    service.set_blocked(conn, uid, True)
    conn.close()
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_session_cookie_flags_over_http(env):
    make_user(env)
    h = session_cookie_header(login(env))
    low = h.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "secure" not in low
    assert "max-age=2592000" in low  # 30 дней


def test_session_cookie_secure_over_https(tmp_path):
    e = build_env(tmp_path, https=True)
    with e.client:
        make_user(e)
        h = session_cookie_header(login(e))
        assert "secure" in h.lower() and "httponly" in h.lower()


def test_session_cookie_secure_behind_proxy_header(env):
    make_user(env)
    token = csrf_of(env)
    r = env.client.post("/login", data={"csrf_token": token, "email": "ivan@example.test", "password": PASSWORD},
                        headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert "secure" in session_cookie_header(r).lower()


def test_csrf_cookie_flags(env):
    r = env.client.get("/login")
    h = next(h for h in r.headers.get_list("set-cookie") if h.startswith("rabota_csrf="))
    assert "httponly" in h.lower() and "samesite=lax" in h.lower()


def test_session_token_is_random_and_only_hash_in_db(env):
    make_user(env)
    login(env)
    raw = env.client.cookies.get("rabota_session")
    assert raw and len(raw) >= 40
    row = env.query("SELECT token_hash FROM sessions")[0]
    assert raw not in row["token_hash"] and len(row["token_hash"]) == 64


def test_login_rotates_session_and_csrf(env):
    make_user(env)
    login(env)
    first = env.client.cookies.get("rabota_session")
    csrf_before = env.client.cookies.get("rabota_csrf")
    login(env)  # повторный вход из той же вкладки
    assert env.client.cookies.get("rabota_session") != first
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1  # старая сессия удалена
    assert env.client.cookies.get("rabota_csrf") != csrf_before


def test_logout_deletes_session_and_cookie(env):
    login_as(env)
    r = post(env, "/logout", {}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0
    assert env.client.get("/account", follow_redirects=False).status_code == 303
    assert "rabota_session" not in env.client.cookies


def test_logout_requires_post(env):
    login_as(env)
    assert env.client.get("/logout").status_code in (404, 405)
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1


def test_stolen_cookie_dies_after_logout(env):
    login_as(env)
    raw = env.client.cookies.get("rabota_session")
    post(env, "/logout", {}, page="/account")
    env.client.cookies.set("rabota_session", raw)
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_session_is_extended_on_activity_and_cookie_refreshed(env):
    login_as(env)
    env.clock.advance(days=3)
    r = env.client.get("/account")
    assert session_cookie_header(r), "cookie должен обновиться вместе со сроком"
    expires = env.query("SELECT expires_at FROM sessions")[0]["expires_at"]
    assert expires.startswith((env.clock() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"))


def test_session_expires_after_30_days(env):
    login_as(env)
    env.clock.advance(days=30, seconds=5)
    r = env.client.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "rabota_session" not in env.client.cookies  # просроченная cookie убрана


def test_garbage_session_cookie_is_ignored_and_cleared(env):
    env.client.cookies.set("rabota_session", "garbage")
    r = env.client.get("/login")
    assert r.status_code == 200
    assert "rabota_session=" in "".join(r.headers.get_list("set-cookie"))


def test_login_page_redirects_logged_in_user(env):
    login_as(env)
    r = env.client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"
    assert env.client.get("/register", follow_redirects=False).status_code == 303


def test_password_change_in_one_browser_logs_out_the_other(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        make_user(e)
        login(e)
        from fastapi.testclient import TestClient
        other = TestClient(e.app)
        token = other.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
        assert other.post("/login", data={"csrf_token": token, "email": "ivan@example.test", "password": PASSWORD},
                          follow_redirects=False).status_code == 303
        assert other.get("/account").status_code == 200
        r = post(e, "/account/password", {"current_password": PASSWORD, "password": "brand-new-pass-9", "password2": "brand-new-pass-9"}, page="/account")
        assert r.status_code == 303
        assert e.client.get("/account", follow_redirects=False).status_code == 200  # текущая жива
        assert other.get("/account", follow_redirects=False).status_code == 303  # другая закрыта


def test_login_failures_by_email_are_limited(tmp_path):
    e = build_env(tmp_path, rate_limits={"login_email": (3, 900)})
    with e.client:
        make_user(e)
        codes = [login(e, password="wrong-password-1").status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        assert login(e).status_code == 429  # даже верный пароль: ящик под защитой
        e.acc.limiter.reset("login_email", "ivan@example.test")
        assert login(e).status_code == 303


def test_login_failures_by_ip_are_limited(tmp_path):
    e = build_env(tmp_path, rate_limits={"login_ip": (3, 900), "login_email": (100, 900)})
    with e.client:
        codes = [login(e, email=f"u{i}@example.test", password="wrong-password-1").status_code for i in range(5)]
        assert codes == [401, 401, 401, 429, 429]


def test_successful_logins_do_not_count_against_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"login_ip": (2, 900), "login_email": (2, 900)})
    with e.client:
        make_user(e)
        for _ in range(6):
            assert login(e).status_code == 303


def test_login_limit_window_passes(tmp_path):
    e = build_env(tmp_path, rate_limits={"login_email": (2, 900)})
    with e.client:
        make_user(e)
        for _ in range(2):
            login(e, password="wrong-password-1")
        assert login(e).status_code == 429
        # лимитер на своих часах: сдвигаем их
        e.acc.limiter.clock = lambda: 10**9
        assert login(e).status_code == 303
