"""Регистрация по коду из письма: ввод кода, срок, попытки, повторная отправка, лимиты, одинаковые ответы."""
import re

import pytest

from rabota.accounts import codes
from tests.accounts.harness import (
    PASSWORD,
    build_env,
    code_in,
    csrf_of,
    env_impl,
    link_in,
    login,
    post,
    register,
    wrong_code,
)

env = pytest.fixture(env_impl)

EMAIL = "ivan@example.test"


def enter(env, code, email=EMAIL, **extra):
    return post(env, "/register/code", {"email": email, "code": code, **extra}, page="/register/code")


def resend(env, email=EMAIL):
    return post(env, "/register/code/resend", {"email": email}, page="/register/code")


# ------------------------------------------------------------------ страница

def test_code_page_after_register_has_accessible_input(env):
    r = register(env)
    assert r.status_code == 200
    html = r.text
    assert 'name="code"' in html
    assert 'autocomplete="one-time-code"' in html and 'inputmode="numeric"' in html
    assert 'action="/register/code"' in html and 'action="/register/code/resend"' in html
    assert "noindex" in html and "<h1>Введите код из письма</h1>" in html
    assert 'name="csrf_token"' in html
    # адрес известен: поле почты скрытое, но его можно сменить
    assert f'value="{EMAIL}"' in html and 'href="/register"' in html


def test_code_page_without_email_asks_for_it(env):
    r = env.client.get("/register/code")
    assert r.status_code == 200 and 'type="email"' in r.text and 'name="code"' in r.text
    assert "x-robots-tag" in r.headers and "noindex" in r.headers["x-robots-tag"]
    prefilled = env.client.get("/register/code?email=a%40b.ru")
    assert "a@b.ru" in prefilled.text and "Другой адрес" in prefilled.text


def test_code_page_escapes_email(env):
    r = env.client.get('/register/code?email=%22%3E%3Cscript%3Ealert(1)%3C/script%3E')
    assert "<script>alert(1)" not in r.text


# ------------------------------------------------------------------ верный код

def test_correct_code_activates_account(env):
    register(env)
    assert env.user(EMAIL)["email_verified_at"] is None
    assert login(env).status_code == 403  # до кода войти нельзя
    code = code_in(env.mailer.last())
    r = enter(env, code)
    assert r.status_code == 200 and "Почта подтверждена" in r.text
    assert env.user(EMAIL)["email_verified_at"]
    assert login(env, password=PASSWORD).status_code == 303


def test_code_with_spaces_and_dash_is_accepted(env):
    register(env)
    code = code_in(env.mailer.last())
    assert enter(env, f" {code[:3]} {code[3:]} ").status_code == 200
    assert env.user(EMAIL)["email_verified_at"]


def test_email_case_does_not_matter(env):
    register(env)
    assert enter(env, code_in(env.mailer.last()), email="  IVAN@Example.TEST ").status_code == 200


def test_code_is_single_use(env):
    register(env)
    code = code_in(env.mailer.last())
    assert enter(env, code).status_code == 200
    again = enter(env, code)
    assert again.status_code == 400 and "Код не подошёл" in again.text


def test_old_link_still_works_as_fallback_and_kills_code(env):
    register(env)
    mail = env.mailer.last()
    assert env.client.get(link_in(mail)).status_code == 200
    assert env.user(EMAIL)["email_verified_at"]
    assert enter(env, code_in(mail)).status_code == 400  # аккаунт уже подтверждён


def test_code_in_mail_is_not_stored_in_clear(env):
    register(env)
    code = code_in(env.mailer.last())
    row = env.query("SELECT * FROM email_codes")[0]
    assert code not in str(tuple(row)) and len(row["code_hash"]) == 64


# ------------------------------------------------------------------ неверный код, срок, попытки

def test_wrong_code_is_rejected_with_generic_text(env):
    register(env)
    code = code_in(env.mailer.last())
    r = enter(env, wrong_code(code))
    assert r.status_code == 400 and "Код не подошёл или срок его действия истёк" in r.text
    assert 'role="alert"' in r.text and env.user(EMAIL)["email_verified_at"] is None
    assert wrong_code(code) not in r.text.replace('name="code"', "")  # введённое не подсвечивается как верное


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34"])
def test_malformed_code_is_validation_error_and_costs_no_attempt(env, bad):
    register(env)
    r = enter(env, bad)
    assert r.status_code == 422 and "Введите шесть цифр" in r.text
    assert env.query("SELECT attempts FROM email_codes")[0]["attempts"] == 0


def test_code_expires_after_ttl(env):
    register(env)
    code = code_in(env.mailer.last())
    env.clock.advance(seconds=codes.CODE_TTL["verify"].total_seconds() + 1)
    r = enter(env, code)
    assert r.status_code == 400 and "срок его действия истёк" in r.text
    assert env.user(EMAIL)["email_verified_at"] is None


def test_code_works_just_before_expiry(env):
    register(env)
    code = code_in(env.mailer.last())
    env.clock.advance(seconds=codes.CODE_TTL["verify"].total_seconds() - 1)
    assert enter(env, code).status_code == 200


def test_attempts_are_limited_and_correct_code_dies_after_lock(env):
    register(env)
    code = code_in(env.mailer.last())
    bad = wrong_code(code)
    statuses = [enter(env, bad).status_code for _ in range(codes.MAX_ATTEMPTS)]
    assert statuses[:-1] == [400] * (codes.MAX_ATTEMPTS - 1) and statuses[-1] == 429
    r = enter(env, code)  # даже верный код после исчерпания попыток не принимается
    assert r.status_code == 429 and "Слишком много неверных попыток" in r.text
    assert env.user(EMAIL)["email_verified_at"] is None


def test_new_code_after_lock_works(env):
    register(env)
    first = code_in(env.mailer.last())
    for _ in range(codes.MAX_ATTEMPTS):
        enter(env, wrong_code(first))
    env.clock.advance(seconds=61)
    assert resend(env).status_code == 200
    second = code_in(env.mailer.last())
    assert enter(env, second).status_code == 200


def test_attempts_limit_survives_restart_of_memory_limiter(env):
    """Счётчик попыток лежит в базе: сброс лимитов в памяти (перезапуск) не даёт перебирать дальше."""
    register(env)
    code = code_in(env.mailer.last())
    for _ in range(codes.MAX_ATTEMPTS):
        enter(env, wrong_code(code))
    env.acc.limiter._hits.clear()
    assert enter(env, code).status_code in (400, 429)
    assert env.user(EMAIL)["email_verified_at"] is None


def test_ip_failure_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"code_fail_ip": (3, 3600)})
    with e.client:
        register(e)
        bad = wrong_code(code_in(e.mailer.last()))
        out = [enter(e, bad, email=f"u{i}@example.test").status_code for i in range(5)]
        assert out == [400, 400, 400, 429, 429]


# ------------------------------------------------------------------ одинаковые ответы

def test_unknown_email_gets_same_answers_as_known(env):
    register(env)
    known_ok_format = enter(env, "000000" if code_in(env.mailer.last()) != "000000" else "111111")
    ghost = enter(env, "000000", email="ghost@example.test")
    assert known_ok_format.status_code == ghost.status_code == 400
    assert known_ok_format.text.replace(EMAIL, "X") == ghost.text.replace("ghost@example.test", "X")


def test_lock_is_identical_for_known_and_unknown(env):
    register(env)
    for _ in range(codes.MAX_ATTEMPTS):
        known = enter(env, "000000")
        ghost = enter(env, "000000", email="ghost@example.test")
    assert known.status_code == ghost.status_code == 429
    assert known.text.replace(EMAIL, "X") == ghost.text.replace("ghost@example.test", "X")


def test_register_page_same_for_new_and_existing_email(env):
    register(env, "known@example.test")
    enter(env, code_in(env.mailer.last()), email="known@example.test")
    env.mailer.outbox.clear()
    a = register(env, "known@example.test")
    b = register(env, "fresh@example.test")
    assert a.status_code == b.status_code == 200
    assert a.text.replace("known@example.test", "X") == b.text.replace("fresh@example.test", "X")
    mails = {m.to: m for m in env.mailer.outbox}
    assert "уже зарегистрированы" in mails["known@example.test"].subject
    assert not re.search(r"(?<!\d)\d{6}(?!\d)", mails["known@example.test"].body)
    assert re.search(r"(?<!\d)\d{6}(?!\d)", mails["fresh@example.test"].body)


def test_existing_email_code_attempt_does_not_reveal_account(env):
    register(env, "known@example.test")
    enter(env, code_in(env.mailer.last()), email="known@example.test")
    register(env, "known@example.test")  # письмо «уже зарегистрированы»
    r = enter(env, "123456", email="known@example.test")
    assert r.status_code == 400 and "Код не подошёл" in r.text


# ------------------------------------------------------------------ повторная отправка и лимиты

def test_resend_too_soon_is_refused_for_everyone(env):
    register(env)
    resend(env, "ghost@example.test")  # неизвестная почта: письма нет, но минута пошла и для неё
    env.mailer.outbox.clear()
    known = resend(env)
    ghost = resend(env, "ghost@example.test")
    assert known.status_code == ghost.status_code == 200
    assert "через минуту" in known.text
    assert known.text.replace(EMAIL, "X") == ghost.text.replace("ghost@example.test", "X")
    assert env.mailer.outbox == []


def test_resend_after_a_minute_sends_new_code_and_kills_old(env):
    register(env)
    old = code_in(env.mailer.last())
    env.clock.advance(seconds=61)
    r = resend(env)
    assert r.status_code == 200 and "новый код" in r.text
    assert len(env.mailer.outbox) == 2
    new = code_in(env.mailer.last())
    if new != old:
        assert enter(env, old).status_code == 400
    assert enter(env, new).status_code == 200


def test_resend_hourly_limit_per_email(env):
    register(env)
    sent = 1
    for _ in range(12):
        env.clock.advance(seconds=61)
        before = len(env.mailer.outbox)
        resend(env)
        sent += len(env.mailer.outbox) - before
    assert sent == codes.MAX_PER_HOUR
    last = resend(env)
    assert "Попробуйте через час" in last.text or "через минуту" in last.text


def test_resend_limit_resets_after_an_hour(env):
    register(env)
    for _ in range(10):
        env.clock.advance(seconds=61)
        resend(env)
    env.mailer.outbox.clear()
    env.clock.advance(hours=2)
    resend(env)
    assert len(env.mailer.outbox) == 1


def test_resend_ip_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"code_send_ip": (2, 3600), "register_ip": (50, 3600)})
    with e.client:
        out = [resend(e, f"u{i}@example.test").status_code for i in range(4)]
        assert out == [200, 200, 429, 429]


def test_resend_for_confirmed_or_unknown_sends_nothing(env):
    register(env)
    enter(env, code_in(env.mailer.last()))
    env.mailer.outbox.clear()
    env.clock.advance(seconds=61)
    resend(env)
    resend(env, "ghost@example.test")
    assert env.mailer.outbox == []


def test_resend_bad_email_format(env):
    assert resend(env, "oops").status_code == 422


def test_smtp_failure_on_resend_does_not_crash(tmp_path):
    from tests.accounts.harness import MemoryMailer
    e = build_env(tmp_path, mailer=MemoryMailer(fail=True))
    with e.client:
        register(e)
        e.clock.advance(seconds=61)
        assert resend(e).status_code == 200


def test_register_again_while_pending_replaces_password_and_code(env):
    register(env, password="first-secret-11")
    first = code_in(env.mailer.last())
    register(env, password="second-secret-22")
    second = code_in(env.mailer.last())
    if first != second:
        assert enter(env, first).status_code == 400
    assert enter(env, second).status_code == 200
    assert login(env, password="second-secret-22").status_code == 303
    assert login(env, password="first-secret-11").status_code == 401


def test_csrf_is_required_for_code_pages(env):
    csrf_of(env, "/register/code")
    for path in ("/register/code", "/register/code/resend", "/forgot/code", "/forgot/code/resend"):
        assert env.client.post(path, data={"email": EMAIL, "code": "123456"}).status_code == 403
