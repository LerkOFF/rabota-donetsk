"""Регистрация и подтверждение почты через сайт."""
import pytest

from tests.accounts.harness import PASSWORD, env_impl, link_in, login, post, reg_data, register, token_in

env = pytest.fixture(env_impl)


def test_register_page_has_all_fields(env):
    r = env.client.get("/register")
    assert r.status_code == 200
    html = r.text
    for needle in ('name="email"', 'name="password"', 'name="name"', 'value="job_seeker"', 'value="employer"',
                   'name="consent"', 'name="website"', 'name="csrf_token"', "noindex"):
        assert needle in html
    assert 'autocomplete="new-password"' in html and 'type="password"' in html


def test_register_creates_account_and_sends_verification_mail(env):
    r = register(env, "Ivan@Example.test")
    assert r.status_code == 200
    assert "Проверьте почту" in r.text
    assert len(env.mailer.outbox) == 1
    mail = env.mailer.outbox[0]
    assert mail.to == "ivan@example.test"
    assert link_in(mail).startswith("/verify/")
    assert "https://example.test/verify/" in mail.body
    user = env.user("ivan@example.test")
    assert user["role"] == "job_seeker" and user["email_verified_at"] is None and user["consent_at"]


def test_register_employer_role(env):
    register(env, "boss@example.test", role="employer", name="Директор")
    row = env.user("boss@example.test")
    assert (row["role"], row["name"]) == ("employer", "Директор")


def test_email_is_normalized(env):
    register(env, "  MiXed@Example.TEST ")
    assert env.user("mixed@example.test")
    assert env.mailer.outbox[0].to == "mixed@example.test"


def test_password_is_not_in_database_or_mail(env):
    register(env)
    assert PASSWORD not in env.user("ivan@example.test")["password_hash"]
    assert PASSWORD not in env.mailer.outbox[0].body


@pytest.mark.parametrize("over,field", [
    ({"email": "not-an-email"}, "email"),
    ({"password": "short"}, "password"),
    ({"password": "password123"}, "password"),
    ({"password": "ivan@example.test"}, "password"),
    ({"name": "   "}, "name"),
    ({"role": "admin"}, "role"),
    ({"role": ""}, "role"),
    ({"consent": ""}, "consent"),
])
def test_validation_errors_block_registration(env, over, field):
    r = register(env, **over)
    assert r.status_code == 422
    assert f'id="r-{field}"' in r.text or field == "role"
    assert "error" in r.text
    assert env.query("SELECT COUNT(*) AS n FROM users")[0]["n"] == 0
    assert env.mailer.outbox == []


def test_consent_required_text_and_nothing_stored(env):
    data = reg_data()
    del data["consent"]
    r = post(env, "/register", data, page="/register")
    assert r.status_code == 422 and "согласия" in r.text
    assert env.user("ivan@example.test") is None


def test_form_is_refilled_but_never_with_password(env):
    r = register(env, "ivan@example.test", password="short", name="Иван Петров")
    assert 'value="ivan@example.test"' in r.text and 'value="Иван Петров"' in r.text
    assert "short" not in r.text


def test_honeypot_pretends_success_and_does_nothing(env):
    r = register(env, website="http://spam.test")
    assert r.status_code == 200 and "Проверьте почту" in r.text
    assert env.user("ivan@example.test") is None and env.mailer.outbox == []


def test_existing_email_gets_same_response_and_different_mail(env):
    register(env, "ivan@example.test")
    env.client.get(link_in(env.mailer.last()))  # подтвердили
    env.mailer.outbox.clear()
    fresh = register(env, "new@example.test")
    again = register(env, "ivan@example.test", name="Иван")
    assert fresh.status_code == again.status_code == 200
    # страницы различаются только адресом почты
    assert fresh.text.replace("new@example.test", "X") == again.text.replace("ivan@example.test", "X").replace("Иван", "Иван")
    mails = {m.to: m for m in env.mailer.outbox}
    assert "уже зарегистрированы" in mails["ivan@example.test"].subject
    assert "/verify/" not in mails["ivan@example.test"].body
    assert "/verify/" in mails["new@example.test"].body


def test_existing_email_does_not_change_account(env):
    register(env, "ivan@example.test", name="Иван")
    env.client.get(link_in(env.mailer.last()))
    register(env, "ivan@example.test", name="Мошенник", role="employer", password="other-password-55")
    row = env.user("ivan@example.test")
    assert (row["name"], row["role"]) == ("Иван", "job_seeker")
    assert login(env, password=PASSWORD).status_code == 303


def test_smtp_failure_does_not_crash_and_shows_message(tmp_path):
    from tests.accounts.harness import MemoryMailer, build_env
    e = build_env(tmp_path, mailer=MemoryMailer(fail=True))
    with e.client:
        r = register(e)
        assert r.status_code == 200
        assert "Не удалось отправить письмо" in r.text
        assert e.user("ivan@example.test") is not None  # аккаунт создан, можно запросить письмо ещё раз


# ------------------------------------------------------------------ подтверждение

def test_verify_link_confirms_email(env):
    register(env)
    r = env.client.get(link_in(env.mailer.last()))
    assert r.status_code == 200 and "Почта подтверждена" in r.text
    assert env.user("ivan@example.test")["email_verified_at"]


def test_verify_link_is_single_use(env):
    register(env)
    path = link_in(env.mailer.last())
    assert env.client.get(path).status_code == 200
    r = env.client.get(path)
    assert r.status_code == 400 and "Ссылка не подошла" in r.text


def test_verify_link_expires_after_24_hours(env):
    register(env)
    path = link_in(env.mailer.last())
    env.clock.advance(hours=24, seconds=1)
    r = env.client.get(path)
    assert r.status_code == 400
    assert env.user("ivan@example.test")["email_verified_at"] is None


def test_verify_garbage_token(env):
    for path in ("/verify/abc", "/verify/" + "x" * 300, "/verify/%27%20OR%201%3D1"):
        assert env.client.get(path).status_code == 400


def test_verify_does_not_log_in(env):
    register(env)
    env.client.get(link_in(env.mailer.last()))
    assert "rabota_session" not in env.client.cookies
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_verify_stores_only_hash(env):
    register(env)
    token = token_in(env.mailer.last())
    rows = env.query("SELECT token_hash FROM email_tokens")
    assert rows and all(token not in r["token_hash"] for r in rows) and len(rows[0]["token_hash"]) == 64


def test_resend_form_and_flow(env):
    register(env)
    first = env.mailer.last()
    env.mailer.outbox.clear()
    r = post(env, "/verify/resend", {"email": "ivan@example.test"}, page="/verify/resend")
    assert r.status_code == 200 and "Письмо отправлено" in r.text
    assert len(env.mailer.outbox) == 1
    second = env.mailer.last()
    assert token_in(first) != token_in(second)
    assert env.client.get(link_in(first)).status_code == 400  # прежняя ссылка погашена
    assert env.client.get(link_in(second)).status_code == 200


def test_resend_gives_same_answer_for_unknown_and_confirmed(env):
    register(env, "known@example.test")
    env.client.get(link_in(env.mailer.last()))
    env.mailer.outbox.clear()
    a = post(env, "/verify/resend", {"email": "nobody@example.test"}, page="/verify/resend")
    b = post(env, "/verify/resend", {"email": "known@example.test"}, page="/verify/resend")
    assert a.status_code == b.status_code == 200 and a.text == b.text
    assert env.mailer.outbox == []


def test_resend_bad_email_is_validation_error(env):
    r = post(env, "/verify/resend", {"email": "oops"}, page="/verify/resend")
    assert r.status_code == 422


def test_resend_page_prefills_email(env):
    r = env.client.get("/verify/resend?email=a%40b.ru")
    assert 'value="a@b.ru"' in r.text


def test_resend_is_rate_limited_per_email_silently(env):
    register(env)
    env.mailer.outbox.clear()
    for _ in range(6):
        r = post(env, "/verify/resend", {"email": "ivan@example.test"}, page="/verify/resend")
        assert r.status_code == 200
    assert len(env.mailer.outbox) == 3  # лимит 3 в час на почту, ответ при этом одинаковый
    env.clock.advance(hours=2)  # лимит считает по своим часам, но токен по часам теста
