"""Страницы привязки Telegram и входа через бота: профиль, /login, поток входа, привязка к браузеру, лимиты, CSRF."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from rabota.accounts import service
from rabota.accounts.mailer import MemoryMailer
from rabota.accounts.ratelimit import RateLimiter
from rabota.telegram_bot import bot
from rabota.web.app import create_app
from tests.accounts.harness import SECRET, SITE, START, Clock, Env, csrf_of, login, make_user, post

EMAIL = "ivan@example.test"
BOT = "test_bot"


def build_env(tmp_path, rate_limits: dict | None = None) -> Env:
    """Приложение на временной базе с управляемыми часами и перехватом писем."""
    db_path = tmp_path / "auth.db"
    mail, clock = MemoryMailer(), Clock()
    app = create_app(db_path, site_url=SITE, auth_options={
        "mailer": mail, "secret": SECRET, "clock": clock,
        "limiter": RateLimiter(rate_limits, clock=lambda: (clock.current - START).total_seconds())})
    return Env(app, TestClient(app, base_url="http://testserver"), mail, clock, db_path, app.state.accounts)


@pytest.fixture
def tg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", BOT)
    e = build_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def plain_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    e = build_env(tmp_path)
    with e.client:
        yield e


def press_start(env, param, tg_id=500, username="ivan_tg"):
    """Человек нажал Start в боте: те же шаги, что у процесса бота, без сети."""
    conn = env.db()
    try:
        update = {"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": tg_id, "type": "private"}, "text": f"/start {param}",
            "from": {"id": tg_id, "username": username},
        }}
        (reply,) = bot.plan(conn, update, site_url=SITE, now=env.clock())
        return reply.text
    finally:
        conn.close()


def start_param(html: str) -> str:
    m = re.search(rf"https://t\.me/{BOT}\?start=([A-Za-z0-9_-]+)", html)
    assert m, html
    return m.group(1)


def link_account(env, email=EMAIL, tg_id=500):
    """Войти, привязать Telegram через профиль и бота."""
    uid = make_user(env, email)
    assert login(env, email).status_code == 303
    r = post(env, "/account/telegram/link", {}, page="/account")
    assert r.status_code == 200
    assert "Готово" in press_start(env, start_param(r.text), tg_id)
    return uid


def new_browser(env) -> TestClient:
    return TestClient(env.app, base_url="http://testserver")


def token_of(client, path="/login") -> str:
    """Токен формы именно этого браузера (он привязан к его cookie)."""
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m
    return m.group(1)


def start_login_flow(client, env, nxt=""):
    token = token_of(client)
    return client.post("/login/telegram", data={"csrf_token": token, "next": nxt}, follow_redirects=False)


# ------------------------------------------------------------------ кнопка на /login

def test_login_has_no_telegram_button_without_bot(plain_env):
    r = plain_env.client.get("/login")
    assert r.status_code == 200 and "Войти через Telegram" not in r.text


def test_login_has_telegram_button_when_bot_is_ready(tg_env):
    r = tg_env.client.get("/login")
    assert "Войти через Telegram" in r.text and 'action="/login/telegram"' in r.text
    assert r.text.count("<h1") == 1


def test_login_hides_button_when_bot_is_switched_off_in_settings(tg_env):
    conn = tg_env.db()
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('tg_bot_enabled', '0', 'x')")
    conn.commit()
    conn.close()
    assert "Войти через Telegram" not in tg_env.client.get("/login").text


def test_start_login_when_unavailable_shows_state_and_sets_no_cookie(plain_env):
    r = start_login_flow(plain_env.client, plain_env)
    assert r.status_code == 503 and "Вход через Telegram сейчас недоступен" in r.text
    assert "rabota_tglogin" not in r.headers.get("set-cookie", "")
    assert plain_env.query("SELECT * FROM tg_login_requests") == []
    assert plain_env.client.get("/login/telegram").status_code == 503
    assert plain_env.client.get("/login/telegram/status").json() == {"state": "unavailable"}


# ------------------------------------------------------------------ вход

def test_full_login_through_telegram(tg_env):
    uid = link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        r = start_login_flow(c, tg_env, "/alerts")
        assert r.status_code == 303 and r.headers["location"] == "/login/telegram?next=/alerts"
        assert "rabota_tglogin" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
        page = c.get("/login/telegram?next=/alerts")
        assert page.status_code == 200 and "Подтвердите вход в Telegram" in page.text and "Я нажал Start" in page.text
        param = start_param(page.text)
        assert param.startswith("login_")
        assert c.get("/login/telegram/status").json() == {"state": "waiting"}
        # пока не нажали Start, сессию не дают
        token = token_of(c)
        early = c.post("/login/telegram/complete", data={"csrf_token": token, "next": "/alerts"})
        assert early.status_code == 200 and "Подтвердите вход" in early.text and "rabota_session" not in c.cookies
        assert "Вход подтверждён" in press_start(tg_env, param)
        status = c.get("/login/telegram/status")
        assert status.json() == {"state": "confirmed"} and status.headers["cache-control"] == "no-store"
        done = c.post("/login/telegram/complete", data={"csrf_token": token, "next": "/alerts"}, follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"] == "/alerts"
        assert "rabota_session" in c.cookies
        assert c.get("/account").status_code == 200
        sessions = tg_env.query("SELECT user_id FROM sessions")
        assert [s["user_id"] for s in sessions][-1] == uid
        # одноразово: вход уже использован, значение в cookie больше ничего не даёт
        again = c.post("/login/telegram/complete", data={"csrf_token": token_of(c), "next": ""}, follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"] == "/login"


def test_login_page_never_contains_the_secret_cookie_value(tg_env):
    link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        nonce = c.cookies.get("rabota_tglogin")
        assert nonce
        assert nonce not in c.get("/login/telegram").text
        assert tg_env.query("SELECT * FROM tg_login_requests")[0]["code_hash"] != nonce


def test_other_browser_cannot_pick_up_the_login(tg_env):
    link_account(tg_env)
    victim = new_browser(tg_env)
    thief = new_browser(tg_env)
    with victim, thief:
        start_login_flow(victim, tg_env)
        param = start_param(victim.get("/login/telegram").text)
        press_start(tg_env, param)
        # у второго браузера нет значения из cookie
        assert thief.get("/login/telegram/status").json() == {"state": "none"}
        r = thief.post("/login/telegram/complete", data={"csrf_token": token_of(thief)}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login" and "rabota_session" not in thief.cookies
        # чужой код из ссылки, подставленный вместо значения из cookie, тоже не подходит
        thief.cookies.set("rabota_tglogin", param.removeprefix("login_"), path="/login/telegram")
        r = thief.post("/login/telegram/complete", data={"csrf_token": token_of(thief)}, follow_redirects=False)
        assert "rabota_session" not in thief.cookies and r.headers["location"] == "/login"
        # а хозяину браузера вход достаётся
        r = victim.post("/login/telegram/complete", data={"csrf_token": token_of(victim)}, follow_redirects=False)
        assert r.status_code == 303 and "rabota_session" in victim.cookies


def test_login_requires_csrf_token(tg_env):
    r = tg_env.client.post("/login/telegram", data={})
    assert r.status_code == 403
    assert tg_env.query("SELECT * FROM tg_login_requests") == []
    link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        assert c.post("/login/telegram/complete", data={}).status_code == 403
        assert c.post("/login/telegram/complete", data={"csrf_token": "bad"}).status_code == 403


def test_unlinked_telegram_sees_how_to_link_and_no_account_is_created(tg_env):
    make_user(tg_env, EMAIL)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        reply = press_start(tg_env, start_param(c.get("/login/telegram").text), tg_id=777)
        assert "Сначала привяжите Telegram в профиле на сайте" in reply
        assert c.get("/login/telegram/status").json() == {"state": "unlinked"}
        page = c.get("/login/telegram")
        assert "не привязан" in page.text and "Начать заново" in page.text
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c)}, follow_redirects=False)
        assert r.status_code == 200 and "rabota_session" not in c.cookies
    assert len(tg_env.query("SELECT id FROM users")) == 1


def test_blocked_account_cannot_sign_in_through_telegram(tg_env):
    uid = link_account(tg_env)
    tg_env.client.post("/logout", data={"csrf_token": csrf_of(tg_env, "/account")})
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        param = start_param(c.get("/login/telegram").text)
        conn = tg_env.db()
        service.set_blocked(conn, uid, True)
        conn.close()
        assert "заблокирован" in press_start(tg_env, param)
        assert c.get("/login/telegram/status").json() == {"state": "blocked"}
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c)}, follow_redirects=False)
        assert r.status_code == 403 and service.BLOCKED_TEXT in r.text and "rabota_session" not in c.cookies


def test_blocked_between_confirm_and_complete(tg_env):
    uid = link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        press_start(tg_env, start_param(c.get("/login/telegram").text))
        conn = tg_env.db()
        service.set_blocked(conn, uid, True)
        conn.close()
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c)}, follow_redirects=False)
        assert r.status_code == 403 and "rabota_session" not in c.cookies


def test_login_expires_after_five_minutes(tg_env):
    link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        param = start_param(c.get("/login/telegram").text)
        tg_env.clock.advance(minutes=6)
        assert c.get("/login/telegram/status").json() == {"state": "expired"}
        page = c.get("/login/telegram")
        assert page.status_code == 410 and "Время вышло" in page.text and "Начать заново" in page.text
        assert "уже не действует" in press_start(tg_env, param)
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c)}, follow_redirects=False)
        assert "rabota_session" not in c.cookies and r.status_code in (303, 410)


def test_unsafe_next_is_dropped(tg_env):
    link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        r = start_login_flow(c, tg_env, "//evil.example/x")
        assert r.headers["location"] == "/login/telegram"
        press_start(tg_env, start_param(c.get("/login/telegram").text))
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c), "next": "//evil.example/x"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/cabinet"


def test_login_start_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", BOT)
    env = build_env(tmp_path, rate_limits={"tg_login_start_ip": (2, 900)})
    with env.client:
        assert start_login_flow(env.client, env).status_code == 303
        assert start_login_flow(env.client, env).status_code == 303
        assert start_login_flow(env.client, env).status_code == 429
        assert len(env.query("SELECT id FROM tg_login_requests")) == 2


def test_status_polling_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", BOT)
    env = build_env(tmp_path, rate_limits={"tg_login_poll_ip": (3, 600)})
    with env.client:
        codes = [env.client.get("/login/telegram/status").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]


def test_failed_attempts_to_take_login_are_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", BOT)
    env = build_env(tmp_path, rate_limits={"tg_login_fail_ip": (2, 900)})
    with env.client:
        token = csrf_of(env, "/login")
        results = [env.client.post("/login/telegram/complete", data={"csrf_token": token}, follow_redirects=False).status_code
                   for _ in range(4)]
        assert results == [303, 303, 429, 429]


def test_signed_in_user_is_redirected_away(tg_env):
    link_account(tg_env)
    assert tg_env.client.get("/login/telegram", follow_redirects=False).status_code == 303
    assert tg_env.client.get("/login/telegram?next=/alerts", follow_redirects=False).headers["location"] == "/alerts"


def test_admin_signs_in_like_with_password(tg_env):
    make_user(tg_env, "root@example.test", role="employer")
    conn = tg_env.db()
    service.create_admin(conn, "root@example.test", "tulip-garden-77", tg_env.clock())
    conn.close()
    assert login(tg_env, "root@example.test").status_code == 303
    r = post(tg_env, "/account/telegram/link", {}, page="/account")
    press_start(tg_env, start_param(r.text), tg_id=900)
    tg_env.client.post("/logout", data={"csrf_token": csrf_of(tg_env, "/account")})
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        press_start(tg_env, start_param(c.get("/login/telegram").text), tg_id=900)
        r = c.post("/login/telegram/complete", data={"csrf_token": token_of(c)}, follow_redirects=False)
        assert r.status_code == 303 and "rabota_session" in c.cookies


def test_telegram_pages_are_not_cached_and_not_indexed(tg_env):
    link_account(tg_env)
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        page = c.get("/login/telegram")
        assert page.headers["cache-control"] == "no-store"
        assert "noindex" in page.headers["x-robots-tag"] and page.text.count("<h1") == 1
        assert chr(0x2014) not in page.text


# ------------------------------------------------------------------ профиль

def test_account_shows_link_button_when_bot_is_ready(tg_env):
    make_user(tg_env, EMAIL)
    login(tg_env, EMAIL)
    r = tg_env.client.get("/account")
    assert "Привязать Telegram" in r.text and 'action="/account/telegram/link"' in r.text


def test_account_says_unavailable_without_bot(plain_env):
    make_user(plain_env, EMAIL)
    login(plain_env, EMAIL)
    r = plain_env.client.get("/account")
    assert r.status_code == 200 and "Привязка Telegram сейчас недоступна" in r.text and "Привязать Telegram</button>" not in r.text
    r = post(plain_env, "/account/telegram/link", {}, page="/account")
    assert r.status_code == 503 and plain_env.query("SELECT * FROM tg_link_codes") == []


def test_link_flow_in_profile(tg_env):
    make_user(tg_env, EMAIL)
    login(tg_env, EMAIL)
    r = post(tg_env, "/account/telegram/link", {}, page="/account")
    assert r.status_code == 200 and "Открыть Telegram" in r.text and "Ждём, пока вы нажмёте Start" in r.text
    param = start_param(r.text)
    assert param.startswith("link_")
    row = tg_env.query("SELECT * FROM tg_link_codes")[0]
    assert param.removeprefix("link_") not in row["code_hash"] and len(row["code_hash"]) == 64
    assert tg_env.client.get("/account/telegram/status").json() == {"linked": False}
    assert "Готово" in press_start(tg_env, param, tg_id=321, username="ivan_tg")
    assert tg_env.client.get("/account/telegram/status").json() == {"linked": True}
    page = tg_env.client.get("/account?saved=tg_linked")
    assert "Привязан: @ivan_tg" in page.text and "Telegram привязан." in page.text and "Отвязать Telegram" in page.text
    assert "Привязать Telegram</button>" not in page.text
    # после привязки на странице входа для этого человека кнопка есть
    assert "Войти через Telegram" in new_browser(tg_env).get("/login").text


def test_link_code_in_profile_expires(tg_env):
    uid = make_user(tg_env, EMAIL)
    login(tg_env, EMAIL)
    r = post(tg_env, "/account/telegram/link", {}, page="/account")
    param = start_param(r.text)
    tg_env.clock.advance(minutes=16)
    assert "уже не действует" in press_start(tg_env, param)
    assert tg_env.query("SELECT tg_user_id FROM users WHERE id=?", uid)[0][0] is None


def test_link_refused_when_telegram_is_taken_by_another_account(tg_env):
    link_account(tg_env, "first@example.test", tg_id=42)
    tg_env.client.post("/logout", data={"csrf_token": csrf_of(tg_env, "/account")})
    make_user(tg_env, "second@example.test")
    login(tg_env, "second@example.test")
    r = post(tg_env, "/account/telegram/link", {}, page="/account")
    reply = press_start(tg_env, start_param(r.text), tg_id=42)
    assert "уже привязан к другому аккаунту" in reply
    assert tg_env.client.get("/account/telegram/status").json() == {"linked": False}


def test_link_requires_login_and_csrf(tg_env):
    r = tg_env.client.post("/account/telegram/link", data={}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert tg_env.query("SELECT * FROM tg_link_codes") == []
    make_user(tg_env, EMAIL)
    login(tg_env, EMAIL)
    assert tg_env.client.post("/account/telegram/link", data={"csrf_token": "bad"}).status_code == 403
    assert tg_env.client.get("/account/telegram/status").status_code == 200
    guest = new_browser(tg_env)
    with guest:
        assert guest.get("/account/telegram/status", follow_redirects=False).status_code == 303


def test_link_is_rate_limited_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TG_BOT_USERNAME", BOT)
    env = build_env(tmp_path, rate_limits={"tg_link_user": (2, 3600)})
    with env.client:
        make_user(env, EMAIL)
        login(env, EMAIL)
        codes = [post(env, "/account/telegram/link", {}, page="/account").status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_unlink_needs_confirmation_and_works(tg_env):
    link_account(tg_env)
    page = tg_env.client.get("/account")
    assert "Да, отвязать" in page.text
    r = post(tg_env, "/account/telegram/unlink", {}, page="/account")
    assert r.status_code == 422 and tg_env.query("SELECT tg_user_id FROM users")[0][0] == 500
    r = post(tg_env, "/account/telegram/unlink", {"confirm": "1"}, page="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=tg_unlinked"
    assert tg_env.query("SELECT tg_user_id FROM users")[0][0] is None
    assert "Telegram отвязан" in tg_env.client.get("/account?saved=tg_unlinked").text
    # вход через Telegram для этого Telegram больше не проходит
    c = new_browser(tg_env)
    with c:
        start_login_flow(c, tg_env)
        assert "Сначала привяжите" in press_start(tg_env, start_param(c.get("/login/telegram").text), tg_id=500)


def test_unlink_is_refused_when_no_other_way_to_sign_in(tg_env, monkeypatch):
    link_account(tg_env)
    monkeypatch.setattr("rabota.accounts.telegram.can_sign_in_without_telegram", lambda row: False)
    page = tg_env.client.get("/account")
    assert "Отвязать Telegram нельзя" in page.text and "Да, отвязать" not in page.text
    r = post(tg_env, "/account/telegram/unlink", {"confirm": "1"}, page="/account")
    assert r.status_code == 409 and tg_env.query("SELECT tg_user_id FROM users")[0][0] == 500


def test_unlink_requires_csrf(tg_env):
    link_account(tg_env)
    r = tg_env.client.post("/account/telegram/unlink", data={"confirm": "1"})
    assert r.status_code == 403 and tg_env.query("SELECT tg_user_id FROM users")[0][0] == 500


def test_account_page_has_no_long_dash_in_new_texts(tg_env):
    link_account(tg_env)
    assert chr(0x2014) not in tg_env.client.get("/account").text
