"""Зависимости доступа, request.state.user, кабинеты по ролям, подключение к приложению."""
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from rabota.accounts import service
from rabota.accounts.deps import (
    LoginRequired,
    get_current_user,
    require_role,
    require_user,
    require_verified,
)
from rabota.web.auth_routes import install
from tests.accounts.harness import (
    PASSWORD,
    SITE,
    MemoryMailer,
    build_env,
    env_impl,
    login,
    login_as,
    make_user,
)

env = pytest.fixture(env_impl)


@pytest.fixture
def probe(env):
    """Тестовые маршруты со всеми зависимостями поверх настоящего приложения."""
    app = env.app

    @app.get("/_t/user")
    def t_user(user: Annotated[dict[str, Any], Depends(require_user)]):
        return {"id": user["id"], "role": user["role"], "keys": sorted(user)}

    @app.get("/_t/verified")
    def t_verified(user: Annotated[dict[str, Any], Depends(require_verified)]):
        return {"email": user["email"]}

    @app.get("/_t/employer")
    def t_employer(user: Annotated[dict[str, Any], Depends(require_role("employer"))]):
        return {"role": user["role"]}

    @app.get("/_t/staff")
    def t_staff(user: Annotated[dict[str, Any], Depends(require_role("employer", "admin"))]):
        return {"role": user["role"]}

    @app.post("/_t/post")
    def t_post(user: Annotated[dict[str, Any], Depends(require_user)]):
        return {"ok": True}

    @app.get("/_t/state")
    def t_state(request: Request):
        u = request.state.user
        return {"user": None if u is None else u["email"], "csrf": bool(request.state.csrf_token),
                "current": None if get_current_user(request) is None else "yes"}

    return env


def test_guest_gets_none_in_state(probe):
    r = probe.client.get("/_t/state").json()
    assert r == {"user": None, "csrf": True, "current": None}


def test_logged_in_user_in_state(probe):
    login_as(probe)
    r = probe.client.get("/_t/state").json()
    assert r["user"] == "ivan@example.test" and r["current"] == "yes"


def test_user_dict_has_no_password_hash(probe):
    login_as(probe)
    keys = probe.client.get("/_t/user").json()["keys"]
    assert "password_hash" not in keys
    assert {"id", "email", "role", "name", "phone", "verified", "is_admin", "email_verified_at", "status"} <= set(keys)


def test_require_user_redirects_guest_to_login_with_next(probe):
    r = probe.client.get("/_t/user?x=1&y=2", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/_t/user%3Fx%3D1%26y%3D2"


def test_next_from_guard_leads_back_after_login(probe):
    make_user(probe)
    loc = probe.client.get("/_t/user", follow_redirects=False).headers["location"]
    r = login(probe, next=loc.split("next=")[1].replace("%2F", "/"))
    assert r.status_code == 303 and r.headers["location"] == "/_t/user"


def test_post_guard_redirects_to_plain_login(probe):
    r = probe.client.post("/_t/post", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_require_verified_ok(probe):
    login_as(probe)
    assert probe.client.get("/_t/verified").json() == {"email": "ivan@example.test"}


def test_require_role_allows_matching_role(probe):
    login_as(probe, "boss@example.test", role="employer")
    assert probe.client.get("/_t/employer").json() == {"role": "employer"}
    assert probe.client.get("/_t/staff").status_code == 200


def test_require_role_forbids_other_role(probe):
    login_as(probe)
    r = probe.client.get("/_t/employer")
    assert r.status_code == 403 and "роли" in r.text
    assert probe.client.get("/_t/staff").status_code == 403


def test_require_role_guest_goes_to_login_not_403(probe):
    r = probe.client.get("/_t/employer", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_require_role_multiple_roles_with_admin(probe):
    conn = probe.db()
    service.create_admin(conn, "admin@example.test", "admin-pass-phrase-5", probe.clock())
    conn.close()
    login(probe, "admin@example.test", "admin-pass-phrase-5")
    assert probe.client.get("/_t/staff").json() == {"role": "admin"}
    assert probe.client.get("/_t/employer").status_code == 403


def test_require_role_needs_at_least_one_role():
    with pytest.raises(ValueError):
        require_role()


def _fake_request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user), method="GET", url=SimpleNamespace(path="/x", query=""))


def test_unverified_user_is_rejected_by_require_verified_and_role():
    req = _fake_request({"id": 1, "role": "employer", "verified": False})
    with pytest.raises(HTTPException) as exc:
        require_verified(req)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        require_role("employer")(req)


def test_require_user_raises_login_required_for_guest():
    with pytest.raises(LoginRequired) as exc:
        require_user(_fake_request(None))
    assert exc.value.next_url == "/login?next=/x"


def test_get_current_user_without_middleware_reads_cookie(tmp_path):
    """Без middleware (нет request.state.user) пользователь определяется по cookie сам."""
    e = build_env(tmp_path)
    with e.client:
        make_user(e)
        login(e)
        raw = e.client.cookies.get("rabota_session")
    app = FastAPI()
    app.state.accounts = e.acc

    @app.get("/who")
    def who(request: Request):
        u = get_current_user(request)
        return {"email": u["email"] if u else None}

    c = TestClient(app)
    assert c.get("/who").json() == {"email": None}
    c.cookies.set("rabota_session", raw)
    assert c.get("/who").json() == {"email": "ivan@example.test"}


# ------------------------------------------------------------------ кабинет

def test_cabinet_requires_login(env):
    r = env.client.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/cabinet"


def test_cabinet_redirects_seeker_and_shows_stub(env):
    login_as(env, name="Анна")
    r = env.client.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/seeker"
    page = env.client.get("/cabinet/seeker")
    assert page.status_code == 200 and "Кабинет соискателя" in page.text and "Анна" in page.text
    assert 'href="/account"' in page.text


def test_cabinet_redirects_employer_and_shows_stub(env):
    login_as(env, "boss@example.test", role="employer", name="Борис")
    assert env.client.get("/cabinet", follow_redirects=False).headers["location"] == "/cabinet/employer"
    page = env.client.get("/cabinet/employer")
    assert page.status_code == 200 and "Кабинет работодателя" in page.text and "Борис" in page.text


def test_cabinet_pages_forbidden_for_other_role(env):
    login_as(env)
    assert env.client.get("/cabinet/employer").status_code == 403
    env.client.cookies.clear()
    login_as(env, "boss@example.test", role="employer")
    assert env.client.get("/cabinet/seeker").status_code == 403


def test_admin_cabinet_goes_to_account(env):
    conn = env.db()
    service.create_admin(conn, "admin@example.test", "admin-pass-phrase-5", env.clock())
    conn.close()
    login(env, "admin@example.test", "admin-pass-phrase-5")
    assert env.client.get("/cabinet", follow_redirects=False).headers["location"] == "/account"


def test_cabinet_stubs_can_be_turned_off(tmp_path):
    e = build_env(tmp_path, cabinet_stubs=False)
    with e.client:
        login_as(e)
        assert e.client.get("/cabinet/seeker").status_code == 404
        assert e.client.get("/cabinet", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ шапка и шаблоны

def test_header_shows_login_links_for_guest(env):
    html = env.client.get("/about").text
    assert 'href="/login"' in html and 'href="/register"' in html and 'action="/logout"' not in html


def test_header_shows_user_menu_and_logout_form_with_csrf(env):
    login_as(env, name="Анна")
    html = env.client.get("/about").text
    assert "Анна" in html and 'action="/logout"' in html
    assert 'name="csrf_token"' in html


def test_logout_form_from_header_works(env):
    login_as(env)
    html = env.client.get("/about").text
    token = html.split('action="/logout"')[1].split('name="csrf_token" value="')[1].split('"')[0]
    r = env.client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert env.client.get("/account", follow_redirects=False).status_code == 303


def test_install_works_on_bare_app_with_own_templates(tmp_path):
    """Запасной путь: приложение без собственного render, шаблоны берутся из пакета."""
    db = tmp_path / "bare.db"
    app = FastAPI()
    install(app, db_path=db, mailer=MemoryMailer(), site_url=SITE, secret="s" * 40)
    with TestClient(app) as c:
        r = c.get("/register")
        assert r.status_code == 200 and "Регистрация" in r.text and "noindex" in r.text
        assert c.get("/login").status_code == 200
        assert c.get("/forgot").status_code == 200
        assert c.get("/account", follow_redirects=False).status_code == 303


def test_install_reads_defaults_from_settings(tmp_path, monkeypatch):
    from rabota import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "from_settings.db")
    monkeypatch.setenv("MAIL_BACKEND", "file")
    app = FastAPI()
    acc = install(app, secret="s" * 40)
    assert acc.db_path == tmp_path / "from_settings.db" and (tmp_path / "from_settings.db").exists()
    assert type(acc.mailer).__name__ == "FileMailer"
    assert acc.mailer.directory == tmp_path / "outbox"


def test_file_mailer_default_writes_eml_with_link(tmp_path):
    from rabota.accounts.mailer import FileMailer
    db = tmp_path / "f.db"
    app = FastAPI()
    install(app, db_path=db, site_url=SITE, secret="s" * 40, mailer=FileMailer(tmp_path / "outbox"))
    with TestClient(app) as c:
        token = c.get("/register").text.split('name="csrf_token" value="')[1].split('"')[0]
        r = c.post("/register", data={"csrf_token": token, "email": "ivan@example.test", "password": PASSWORD,
                                      "name": "Иван", "role": "job_seeker", "consent": "1"})
        assert r.status_code == 200
    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and b"/verify/" in files[0].read_bytes()
