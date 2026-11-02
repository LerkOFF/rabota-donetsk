"""Сброс пароля: основной путь по коду из письма, запасная ссылка /reset/<токен> для уже выданных писем."""
import pytest

from rabota.accounts import codes, service, tokens
from tests.accounts.harness import (
    PASSWORD,
    build_env,
    code_in,
    env_impl,
    login,
    login_as,
    make_user,
    post,
    wrong_code,
)

env = pytest.fixture(env_impl)

NEW = "brand-new-pass-9"
EMAIL = "ivan@example.test"


def ask_reset(env, email=EMAIL):
    return post(env, "/forgot", {"email": email})


def submit(env, code, password=NEW, password2=None, email=EMAIL):
    return post(env, "/forgot/code", {
        "email": email, "code": code, "password": password, "password2": NEW if password2 is None else password2,
    }, page="/forgot/code")


def resend(env, email=EMAIL):
    return post(env, "/forgot/code/resend", {"email": email}, page="/forgot/code")


def old_link(env):
    """Запасная ссылка сброса, выданная до перехода на коды."""
    conn = env.db()
    try:
        uid = service.get_user_by_email(conn, EMAIL)["id"]
        return "/reset/" + tokens.create_email_token(conn, uid, "reset", env.clock())
    finally:
        conn.close()


# ------------------------------------------------------------------ форма и код

def test_forgot_page(env):
    r = env.client.get("/forgot")
    assert r.status_code == 200 and 'name="email"' in r.text and 'name="csrf_token"' in r.text
    assert "noindex" in r.text and "шестизначный код" in r.text


def test_login_page_links_to_forgot(env):
    assert 'href="/forgot"' in env.client.get("/login").text


def test_forgot_shows_code_form_and_sends_code(env):
    make_user(env)
    r = ask_reset(env)
    assert r.status_code == 200
    html = r.text
    assert "Код из письма и новый пароль" in html and 'action="/forgot/code"' in html
    assert 'autocomplete="one-time-code"' in html and 'inputmode="numeric"' in html
    assert html.count('autocomplete="new-password"') == 2
    assert "noindex" in html
    mail = env.mailer.last(EMAIL)
    assert "сброса пароля" in mail.subject and code_in(mail) in mail.subject
    assert "/reset/" not in mail.body  # в письме только код


def test_reset_full_flow(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    r = submit(env, code)
    assert r.status_code == 200 and "Пароль изменён" in r.text
    assert login(env, password=PASSWORD).status_code == 401
    assert login(env, password=NEW).status_code == 303
    assert any("изменён" in m.subject for m in env.mailer.outbox)  # уведомление о смене


def test_reset_page_without_email_asks_for_it(env):
    r = env.client.get("/forgot/code")
    assert r.status_code == 200 and 'type="email"' in r.text and 'name="password"' in r.text
    assert "noindex" in r.headers["x-robots-tag"]


def test_same_response_for_any_email(env):
    make_user(env)
    known = ask_reset(env, EMAIL)
    unknown = ask_reset(env, "ghost@example.test")
    assert known.status_code == unknown.status_code == 200
    assert known.text.replace(EMAIL, "X") == unknown.text.replace("ghost@example.test", "X")
    assert [m.to for m in env.mailer.outbox] == [EMAIL]


def test_forgot_bad_email_format(env):
    assert ask_reset(env, "nope").status_code == 422
    assert env.mailer.outbox == []


# ------------------------------------------------------------------ неверный, просроченный, повторный код

def test_wrong_code_is_rejected_and_password_unchanged(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    r = submit(env, wrong_code(code))
    assert r.status_code == 400 and "Код не подошёл" in r.text
    assert login(env, password=PASSWORD).status_code == 303
    # верный код после одной ошибки ещё работает
    assert submit(env, code).status_code == 200


def test_code_expires(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    env.clock.advance(seconds=codes.CODE_TTL["reset"].total_seconds() + 1)
    r = submit(env, code)
    assert r.status_code == 400 and "срок его действия истёк" in r.text
    assert login(env, password=PASSWORD).status_code == 303


def test_attempts_exhausted_locks_code(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    statuses = [submit(env, wrong_code(code)).status_code for _ in range(codes.MAX_ATTEMPTS)]
    assert statuses == [400] * (codes.MAX_ATTEMPTS - 1) + [429]
    r = submit(env, code)  # верный код после исчерпания не принимается
    assert r.status_code == 429 and "Слишком много неверных попыток" in r.text
    assert login(env, password=PASSWORD).status_code == 303


def test_code_is_single_use(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    assert submit(env, code).status_code == 200
    again = submit(env, code, password="another-pass-77", password2="another-pass-77")
    assert again.status_code == 400
    assert login(env, password=NEW).status_code == 303
    assert login(env, password="another-pass-77").status_code == 401


def test_unknown_email_gets_same_error_as_wrong_code(env):
    make_user(env)
    ask_reset(env)
    known = submit(env, "000000" if code_in(env.mailer.last(EMAIL)) != "000000" else "111111")
    ghost = submit(env, "000000", email="ghost@example.test")
    assert known.status_code == ghost.status_code == 400
    assert known.text.replace(EMAIL, "X") == ghost.text.replace("ghost@example.test", "X")


def test_verify_code_cannot_reset_password(env):
    """Код регистрации и код сброса не взаимозаменяемы."""
    from tests.accounts.harness import register
    register(env, "new@example.test")
    verify_code = code_in(env.mailer.last("new@example.test"))
    r = submit(env, verify_code, email="new@example.test")
    assert r.status_code == 400


def test_reset_validates_new_password_without_spending_attempt(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    weak = submit(env, code, "password1", "password1")
    assert weak.status_code == 422 and "простой" in weak.text
    mismatch = submit(env, code, NEW, NEW + "x")
    assert mismatch.status_code == 422 and "не совпадают" in mismatch.text
    same_as_email = submit(env, code, EMAIL, EMAIL)
    assert same_as_email.status_code == 422
    assert env.query("SELECT attempts FROM email_codes")[0]["attempts"] == 0
    assert submit(env, code).status_code == 200  # код цел


def test_malformed_code_is_validation_error(env):
    make_user(env)
    ask_reset(env)
    for bad in ("", "12", "abcdef"):
        assert submit(env, bad).status_code == 422


def test_reset_for_blocked_user_sends_nothing(env):
    uid = make_user(env)
    conn = env.db()
    service.set_blocked(conn, uid, True)
    conn.close()
    r = ask_reset(env)
    assert r.status_code == 200 and env.mailer.outbox == []


# ------------------------------------------------------------------ сессии

def test_reset_closes_all_sessions(env):
    login_as(env)
    assert env.client.get("/account").status_code == 200
    conn = env.db()
    uid = service.get_user_by_email(conn, EMAIL)["id"]
    service.create_session(conn, uid, env.clock())  # вход с другого устройства
    conn.close()
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 2
    ask_reset(env)
    submit(env, code_in(env.mailer.last(EMAIL)))
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_failed_reset_keeps_sessions(env):
    login_as(env)
    ask_reset(env)
    submit(env, wrong_code(code_in(env.mailer.last(EMAIL))))
    assert env.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1


def test_reset_does_not_log_in_automatically(env):
    make_user(env)
    ask_reset(env)
    submit(env, code_in(env.mailer.last(EMAIL)))
    assert "rabota_session" not in env.client.cookies


def test_reset_verifies_unconfirmed_mailbox(env):
    make_user(env, verified=False)
    ask_reset(env)
    submit(env, code_in(env.mailer.last(EMAIL)))
    assert env.user(EMAIL)["email_verified_at"]


# ------------------------------------------------------------------ повторная отправка и лимиты

def test_resend_too_soon_sends_nothing(env):
    make_user(env)
    ask_reset(env)
    env.mailer.outbox.clear()
    r = resend(env)
    assert r.status_code == 200 and "через минуту" in r.text and env.mailer.outbox == []


def test_resend_after_minute_replaces_code(env):
    make_user(env)
    ask_reset(env)
    first = code_in(env.mailer.last(EMAIL))
    env.clock.advance(seconds=61)
    assert resend(env).status_code == 200
    second = code_in(env.mailer.last(EMAIL))
    if first != second:
        assert submit(env, first).status_code == 400
    assert submit(env, second).status_code == 200


def test_second_forgot_invalidates_first_code(env):
    make_user(env)
    ask_reset(env)
    first = code_in(env.mailer.last(EMAIL))
    env.clock.advance(seconds=61)
    ask_reset(env)
    second = code_in(env.mailer.last(EMAIL))
    if first != second:
        assert submit(env, first).status_code == 400
    assert submit(env, second).status_code == 200


def test_forgot_rate_limit_per_email_is_silent(env):
    make_user(env)
    for _ in range(12):
        assert ask_reset(env).status_code == 200
        env.clock.advance(seconds=61)
    assert len(env.mailer.outbox) == codes.MAX_PER_HOUR
    env.clock.advance(hours=2)
    ask_reset(env)
    assert len(env.mailer.outbox) == codes.MAX_PER_HOUR + 1


def test_forgot_rate_limit_per_ip(tmp_path):
    e = build_env(tmp_path, rate_limits={"forgot_ip": (2, 3600)})
    with e.client:
        out = [ask_reset(e, f"u{i}@example.test").status_code for i in range(4)]
        assert out == [200, 200, 429, 429]


def test_smtp_failure_on_forgot_does_not_crash(tmp_path):
    from tests.accounts.harness import MemoryMailer
    e = build_env(tmp_path, mailer=MemoryMailer(fail=True))
    with e.client:
        make_user(e)
        r = ask_reset(e)
        assert r.status_code == 200 and "Код из письма" in r.text


# ------------------------------------------------------------------ запасная ссылка

def test_old_link_flow_still_works(env):
    make_user(env)
    path = old_link(env)
    assert env.client.get(path).status_code == 200
    r = post(env, path, {"password": NEW, "password2": NEW}, page=path)
    assert r.status_code == 200 and "Пароль изменён" in r.text
    assert login(env, password=NEW).status_code == 303


def test_old_link_is_single_use_and_expires(env):
    make_user(env)
    path = old_link(env)
    assert post(env, path, {"password": NEW, "password2": NEW}, page=path).status_code == 200
    assert env.client.get(path).status_code == 400
    path2 = old_link(env)
    env.clock.advance(hours=1, seconds=1)
    assert env.client.get(path2).status_code == 400


def test_password_change_kills_pending_reset_codes(env):
    make_user(env)
    ask_reset(env)
    code = code_in(env.mailer.last(EMAIL))
    conn = env.db()
    service.change_password(conn, service.get_user_by_email(conn, EMAIL)["id"], "other-secret-55", env.clock())
    conn.close()
    assert submit(env, code).status_code == 400


def test_reset_garbage_token(env):
    assert env.client.get("/reset/garbage").status_code == 400
    r = post(env, "/reset/garbage", {"password": NEW, "password2": NEW}, page="/forgot")
    assert r.status_code == 400


def test_token_guessing_is_throttled(tmp_path):
    e = build_env(tmp_path, rate_limits={"token_ip": (3, 3600)})
    with e.client:
        out = [e.client.get(f"/reset/guess{i}").status_code for i in range(5)]
        assert out == [400, 400, 400, 429, 429]
