"""CSRF, открытый редирект, заголовки, лимиты регистрации, перебор."""
import pytest

from rabota.accounts.deps import safe_next
from tests.accounts.harness import (
    PASSWORD,
    build_env,
    csrf_of,
    env_impl,
    login,
    login_as,
    make_user,
    post,
    reg_data,
    register,
)

env = pytest.fixture(env_impl)

POST_FORMS = [
    ("/register", reg_data()),
    ("/login", {"email": "ivan@example.test", "password": PASSWORD}),
    ("/verify/resend", {"email": "ivan@example.test"}),
    ("/forgot", {"email": "ivan@example.test"}),
    ("/reset/sometoken", {"password": "brand-new-pass-9", "password2": "brand-new-pass-9"}),
]
USER_POST_FORMS = [
    ("/logout", {}),
    ("/account/profile", {"name": "Хакер", "phone": ""}),
    ("/account/password", {"current_password": PASSWORD, "password": "brand-new-pass-9", "password2": "brand-new-pass-9"}),
    ("/account/email", {"new_email": "evil@example.test", "password": PASSWORD}),
    ("/account/delete", {"password": PASSWORD, "confirm": "1"}),
]


@pytest.mark.parametrize("path,data", POST_FORMS)
def test_post_without_csrf_token_is_forbidden(env, path, data):
    env.client.get("/login")  # cookie есть, токена в форме нет
    r = env.client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("path,data", POST_FORMS)
def test_post_with_wrong_csrf_token_is_forbidden(env, path, data):
    env.client.get("/login")
    r = env.client.post(path, data={**data, "csrf_token": "0" * 64}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("path,data", USER_POST_FORMS)
def test_user_forms_need_csrf(env, path, data):
    login_as(env)
    r = env.client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 403
    assert env.user("ivan@example.test")["name"] == "Иван"
    assert env.client.get("/account").status_code == 200  # сессия цела


def test_csrf_token_from_another_visitor_is_rejected(env):
    from fastapi.testclient import TestClient
    attacker = TestClient(env.app)
    stolen = attacker.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
    env.client.get("/login")  # у жертвы своя cookie
    r = env.client.post("/login", data={"csrf_token": stolen, "email": "a@b.ru", "password": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_csrf_without_cookie_is_rejected(env):
    token = csrf_of(env)
    env.client.cookies.clear()
    r = env.client.post("/login", data={"csrf_token": token, "email": "a@b.ru", "password": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_cross_site_origin_is_rejected_even_with_valid_token(env):
    token = csrf_of(env)
    r = env.client.post("/login", data={"csrf_token": token, "email": "a@b.ru", "password": "x"},
                        headers={"origin": "https://evil.test"}, follow_redirects=False)
    assert r.status_code == 403
    r = env.client.post("/login", data={"csrf_token": token, "email": "a@b.ru", "password": "x"},
                        headers={"origin": "null"}, follow_redirects=False)
    assert r.status_code == 403


def test_same_origin_header_is_accepted(env):
    token = csrf_of(env)
    r = env.client.post("/login", data={"csrf_token": token, "email": "a@b.ru", "password": "x"},
                        headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 401


def test_csrf_forbidden_page_is_friendly(env):
    r = env.client.post("/login", data={"email": "a@b.ru", "password": "x"})
    assert r.status_code == 403 and "Форма устарела" in r.text


def test_csrf_token_changes_between_visitors_but_is_stable_per_visitor(env):
    a, b = csrf_of(env), csrf_of(env)
    assert a == b
    from fastapi.testclient import TestClient
    other = TestClient(env.app).get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
    assert other != a


def test_oversized_form_is_rejected(env):
    token = csrf_of(env)
    r = env.client.post("/login", data={"csrf_token": token, "email": "a@b.ru", "password": "x" * 50_000})
    assert r.status_code == 413


# ------------------------------------------------------------------ открытый редирект

@pytest.mark.parametrize("value,expected", [
    ("/account", "/account"),
    ("/cabinet/vacancies?page=2", "/cabinet/vacancies?page=2"),
    ("//evil.test", "/cabinet"),
    ("//evil.test/path", "/cabinet"),
    ("/\\evil.test", "/cabinet"),
    ("/\\\\evil.test", "/cabinet"),
    ("https://evil.test", "/cabinet"),
    ("http://evil.test/", "/cabinet"),
    ("javascript:alert(1)", "/cabinet"),
    ("evil.test", "/cabinet"),
    ("", "/cabinet"),
    (None, "/cabinet"),
    ("/ok\r\nSet-Cookie: x=1", "/cabinet"),
    ("/ok\x00", "/cabinet"),
    ("/" + "a" * 600, "/cabinet"),
    ("/path\\..\\evil", "/cabinet"),
])
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_login_follows_safe_next(env):
    make_user(env)
    r = login(env, next="/account")
    assert r.status_code == 303 and r.headers["location"] == "/account"


@pytest.mark.parametrize("evil", ["https://evil.test", "//evil.test", "/\\evil.test", "javascript:alert(1)"])
def test_login_ignores_open_redirect(env, evil):
    make_user(env)
    r = login(env, next=evil)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"


def test_next_survives_failed_login_in_form(env):
    make_user(env)
    r = login(env, password="wrong-password-1", next="/account")
    assert 'name="next" value="/account"' in r.text
    r = login(env, password="wrong-password-1", next="//evil.test")
    assert "evil.test" not in r.text


def test_login_page_next_is_sanitised_in_form(env):
    assert 'value="/account"' in env.client.get("/login?next=/account").text
    assert "evil.test" not in env.client.get("/login?next=//evil.test").text


def test_guest_redirect_keeps_page_in_next(env):
    r = env.client.get("/account", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/account"


# ------------------------------------------------------------------ заголовки

@pytest.mark.parametrize("path", ["/login", "/register", "/forgot", "/verify/resend", "/verify/x", "/reset/x", "/cabinet", "/account"])
def test_account_pages_are_not_cacheable(env, path):
    r = env.client.get(path, follow_redirects=False)
    assert r.headers["cache-control"] == "no-store"


def test_account_pages_are_noindex(env):
    for path in ("/login", "/register", "/forgot"):
        assert "noindex" in env.client.get(path).text


def test_logged_in_pages_everywhere_are_no_store(env):
    login_as(env)
    assert env.client.get("/about").headers["cache-control"] == "no-store"
    assert env.client.get("/static/site.css").headers["cache-control"].startswith("public")


def test_security_headers_still_present(env):
    r = env.client.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and "content-security-policy" in r.headers


def test_static_requests_skip_account_logic(env):
    r = env.client.get("/static/site.css")
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_public_pages_still_work_and_get_csrf_cookie_only_when_needed(env):
    r = env.client.get("/about")
    assert r.status_code == 200


# ------------------------------------------------------------------ лимиты регистрации

def test_register_rate_limit_per_ip(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_ip": (3, 3600)})
    with e.client:
        codes = [register(e, f"u{i}@example.test").status_code for i in range(5)]
        assert codes == [200, 200, 200, 429, 429]


def test_register_rate_limit_per_email_is_silent(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_email": (2, 3600)})
    with e.client:
        pages = [register(e).text for _ in range(4)]
        assert len({p for p in pages}) == 1  # ответ одинаковый
        assert len(e.mailer.outbox) == 2


def test_register_validation_errors_do_not_burn_email_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_email": (1, 3600)})
    with e.client:
        assert register(e, password="short").status_code == 422
        assert register(e).status_code == 200
        assert len(e.mailer.outbox) == 1


def test_limits_expire_with_time(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_ip": (1, 3600)})
    with e.client:
        assert register(e, "a@example.test").status_code == 200
        assert register(e, "b@example.test").status_code == 429
        e.acc.limiter.clock = lambda: 10**9
        assert register(e, "b@example.test").status_code == 200


def test_proxy_headers_are_ignored_unless_trusted(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_ip": (1, 3600)})
    with e.client:
        for i in range(2):
            token = csrf_of(e, "/register")
            r = e.client.post("/register", data={"csrf_token": token, **reg_data(f"u{i}@example.test")},
                              headers={"x-forwarded-for": f"198.51.100.{i}"})
        assert r.status_code == 429  # подмена заголовка не даёт обойти лимит


def test_trusted_proxy_uses_forwarded_ip(tmp_path):
    e = build_env(tmp_path, rate_limits={"register_ip": (1, 3600)})
    e.acc.trust_proxy = True
    with e.client:
        codes = []
        for i in range(2):
            token = csrf_of(e, "/register")
            r = e.client.post("/register", data={"csrf_token": token, **reg_data(f"u{i}@example.test")},
                              headers={"x-forwarded-for": f"198.51.100.{i}, 10.0.0.1"})
            codes.append(r.status_code)
        assert codes == [200, 200]


# ------------------------------------------------------------------ не раскрываем существование почты

def test_enumeration_register_login_reset_resend(env):
    make_user(env, "known@example.test")
    # регистрация
    a = register(env, "known@example.test")
    b = register(env, "unknown@example.test")
    assert a.text.replace("known@example.test", "X") == b.text.replace("unknown@example.test", "X")
    # вход
    assert login(env, "known@example.test", "wrong-password-1").text.replace("known@", "u@") == \
        login(env, "unknown@example.test", "wrong-password-1").text.replace("unknown@", "u@")
    # сброс и повтор письма
    assert post(env, "/forgot", {"email": "known@example.test"}).text == post(env, "/forgot", {"email": "unknown@example.test"}).text
    assert post(env, "/verify/resend", {"email": "known@example.test"}, page="/verify/resend").text == \
        post(env, "/verify/resend", {"email": "unknown@example.test"}, page="/verify/resend").text


def test_wrong_password_takes_similar_work_for_unknown_email(env, monkeypatch):
    """Для несуществующей почты scrypt всё равно считается: время ответа не выдаёт, есть ли почта."""
    from rabota.accounts import passwords
    calls = []
    real = passwords._derive
    monkeypatch.setattr(passwords, "_derive", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    make_user(env)
    calls.clear()
    login(env, "ivan@example.test", "wrong-password-1")
    known = len(calls)
    calls.clear()
    login(env, "ghost@example.test", "wrong-password-1")
    assert len(calls) >= known >= 1


def test_register_hashes_password_even_for_existing_email(env, monkeypatch):
    from rabota.accounts import passwords
    make_user(env, "known@example.test")
    calls = []
    real = passwords._derive
    monkeypatch.setattr(passwords, "_derive", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    register(env, "known@example.test")
    assert calls, "хеш считается всегда"
