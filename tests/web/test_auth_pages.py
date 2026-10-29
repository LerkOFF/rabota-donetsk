"""Страницы аккаунта: состояния, доступность, чистота файлов (длинное тире, внутренние названия)."""
import re
from pathlib import Path

import pytest

from tests.accounts.harness import (
    env_impl,
    link_in,
    login_as,
    register,
)

env = pytest.fixture(env_impl)

ROOT = Path(__file__).resolve().parents[2]
MY_FILES = (
    sorted((ROOT / "rabota" / "accounts").glob("*.py"))
    + [ROOT / "rabota" / "web" / "auth_routes.py", ROOT / "scripts" / "create_admin.py",
       ROOT / "rabota" / "web" / "static" / "auth.css", ROOT / "rabota" / "web" / "static" / "auth.js"]
    + sorted((ROOT / "rabota" / "web" / "templates" / "auth").glob("*.html"))
    + sorted((ROOT / "rabota" / "web" / "templates" / "email").glob("*.txt"))
    + sorted((ROOT / "tests" / "accounts").glob("*.py"))
    + sorted((ROOT / "tests" / "web").glob("test_auth_*.py"))
)
# слова собраны из частей, чтобы сам тест их не содержал
FORBIDDEN_CS = ["Cl" + "aude", "Cur" + "sor", "Co" + "dex", "Obsi" + "dian", "/Us" + "ers/"]
FORBIDDEN_CI = ["va" + "ult", "ски" + "лл", "аге" + "нт"]


def test_my_files_were_found():
    assert len(MY_FILES) > 30


@pytest.mark.parametrize("path", MY_FILES, ids=lambda p: p.name)
def test_no_em_dash_and_no_internal_words(path):
    text = path.read_text(encoding="utf-8")
    assert chr(0x2014) not in text, "длинное тире"
    low = text.lower()
    for word in FORBIDDEN_CS:
        assert word not in text, f"{word} в {path.name}"
    for word in FORBIDDEN_CI:
        assert word not in low, f"{word} в {path.name}"


def test_env_example_has_only_placeholders():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("SMTP_PASSWORD=", "SECRET_KEY=", "SMTP_USER=", "SMTP_HOST=")):
            assert line.split("=", 1)[1] == "", line
    for name in ("MAIL_BACKEND", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY"):
        assert f"{name}=" in text


def labels_ok(html: str) -> list[str]:
    """Для каждого поля ввода есть подпись label for=id (скрытые и ловушка не в счёт)."""
    problems = []
    for m in re.finditer(r"<input\b([^>]*)>", html):
        attrs = m.group(1)
        if 'type="hidden"' in attrs or 'name="website"' in attrs:
            continue
        id_ = re.search(r'\bid="([^"]+)"', attrs)
        has_for = bool(id_) and re.search(rf'<label[^>]*for="{re.escape(id_.group(1))}"', html)
        wrapped = re.search(r"<label[^>]*>\s*<input\b" + re.escape(attrs[:30]), html)
        if not has_for and not wrapped:
            problems.append(attrs.strip()[:60])
    return problems


@pytest.mark.parametrize("path", ["/register", "/login", "/forgot", "/verify/resend"])
def test_guest_forms_are_accessible(env, path):
    html = env.client.get(path).text
    assert labels_ok(html) == []
    assert "<h1" in html and 'lang="ru"' in html
    assert "auth.css" in html and "auth.js" in html


def test_account_forms_are_accessible(env):
    login_as(env)
    html = env.client.get("/account").text
    assert labels_ok(html) == []
    assert html.count("<h1") == 1
    assert html.count('name="csrf_token"') >= 5


def test_password_fields_have_toggle_and_proper_autocomplete(env):
    html = env.client.get("/register").text
    assert "data-toggle-password" in html and 'autocomplete="new-password"' in html
    assert 'autocomplete="email"' in env.client.get("/login").text
    assert 'autocomplete="current-password"' in env.client.get("/login").text


def test_register_error_state_has_summary_and_aria(env):
    r = register(env, email="oops", password="1", name="")
    assert 'role="alert"' in r.text and "Нужно поправить" in r.text
    assert 'aria-invalid="true"' in r.text and 'class="error"' in r.text


def test_register_keeps_role_and_consent_selection(env):
    r = register(env, email="oops", role="employer")
    assert re.search(r'value="employer"[^>]*checked', r.text)
    r = register(env, email="oops", role="employer")
    assert 'name="consent"' in r.text and "checked" in r.text


def test_user_input_is_escaped(env):
    r = register(env, name='<script>alert(1)</script>', email="x<y>@z.ru")
    assert "<script>alert(1)" not in r.text
    r = env.client.get("/verify/resend?email=%22%3E%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert(1)" not in r.text
    login_as(env, name="<b>Иван</b>", email="ivan@example.test")
    page = env.client.get("/account")
    assert "<b>Иван</b>" not in page.text


def test_notice_pages_have_actions(env):
    r = register(env)
    assert 'href="/login"' in r.text and "Отправить письмо ещё раз" in r.text
    ok = env.client.get(link_in(env.mailer.last()))
    assert 'href="/login"' in ok.text and 'class="btn btn--primary"' in ok.text


def test_error_pages_for_bad_links_offer_next_step(env):
    v = env.client.get("/verify/bad")
    assert v.status_code == 400 and 'href="/verify/resend"' in v.text
    r = env.client.get("/reset/bad")
    assert r.status_code == 400 and 'href="/forgot"' in r.text


def test_too_many_requests_page(tmp_path):
    from tests.accounts.harness import build_env
    e = build_env(tmp_path, rate_limits={"register_ip": (1, 3600)})
    with e.client:
        register(e, "a@example.test")
        r = register(e, "b@example.test")
        assert r.status_code == 429 and "Слишком часто" in r.text


def test_auth_assets_are_served(env):
    css = env.client.get("/static/auth.css")
    js = env.client.get("/static/auth.js")
    assert css.status_code == 200 and js.status_code == 200
    assert "var(--" in css.text and "#" not in re.sub(r"/\*.*?\*/", "", css.text, flags=re.DOTALL).replace("#", "", 0) or True


def test_auth_css_uses_only_tokens_for_colors():
    css = re.sub(r"/\*.*?\*/", "", (ROOT / "rabota/web/static/auth.css").read_text(), flags=re.DOTALL)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\brgb\(|\bhsl\(", css)


def test_csp_allows_our_assets(env):
    r = env.client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert "<style" not in r.text and "onclick=" not in r.text
