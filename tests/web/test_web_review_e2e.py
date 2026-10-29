"""Приёмка интерфейса в браузере: размеры целей нажатия, слова в плитках, подсказки в строке поиска, порядок заголовков.

Сервер на свободном порту, база временная, пользователи созданы напрямую. Запуск: .venv/bin/pytest -m e2e tests/web/test_web_review_e2e.py
"""
import importlib.util
import itertools
import socket
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from rabota.accounts import service
from rabota.db import connect
from rabota.web.app import create_app

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Demo-pass-2026!"
MIN_TAP = 44


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_user(conn: sqlite3.Connection, email: str, name: str, role: str) -> int:
    now = datetime.now(UTC)
    if role == "admin":
        return service.create_admin(conn, email, PASSWORD, now)
    reg = service.register(conn, email=email, password=PASSWORD, name=name, role=role, consent=True, now=now)
    conn.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (now.isoformat(), reg.user_id))
    conn.commit()
    return reg.user_id


@pytest.fixture(scope="module")
def db_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("review") / "demo.db"
    _seed_module().seed(path)
    conn = connect(path)
    seeker = _make_user(conn, "seeker@example.test", "Анна Петрова", "job_seeker")
    employer = _make_user(conn, "employer@example.test", "Олег Кравец", "employer")
    _make_user(conn, "admin@example.test", "Админ", "admin")
    vac = conn.execute("SELECT id FROM vacancies WHERE origin = 'direct' ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE vacancies SET owner_user_id = ? WHERE origin = 'direct'", (employer,))
    now = datetime.now(UTC).isoformat(timespec="seconds")
    app_id = conn.execute(
        "INSERT INTO applications(vacancy_id, user_id, cover_letter, status, created_at) VALUES (?,?,?,?,?)",
        (vac, seeker, "Здравствуйте! Есть опыт, готова выйти завтра.", "new", now),
    ).lastrowid
    conv = conn.execute(
        "INSERT INTO conversations(employer_user_id, seeker_user_id, vacancy_id, application_id, started_by, created_at, last_message_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (employer, seeker, vac, app_id, seeker, now, now),
    ).lastrowid
    conn.execute("INSERT INTO messages(conversation_id, sender_user_id, body, created_at) VALUES (?,?,?,?)", (conv, seeker, "Добрый день!", now))
    conn.execute(
        "INSERT INTO vacancies(profession, title_raw, category, city, district, employer, summary, contacts, source_type, source, source_url,"
        " external_id, published_at, last_seen_at, kind, dedupe_key, status, origin, moderation_status, pay_min, pay_max, pay_period)"
        " VALUES ('официант','Официант','общепит','Донецк','Ворошиловский','Кофейня','Описание.','{\"phones\": [\"+79490000099\"]}',"
        "'site','razmestit','','p1',?,?,'vacancy','d-p1','hidden','direct','pending',30000,40000,'месяц')",
        (now, now),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def base_url(db_file):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(db_file, site_url=f"http://127.0.0.1:{port}", rate_limits={"complaint": (100, 60), "post": (100, 60)})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("сервер не запустился")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium недоступен: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def sessions(browser, base_url):
    """Состояние входа каждой роли: один вход на модуль, лимиты входа не мешают."""
    states: dict[str, dict | None] = {"guest": None}
    for role in ("seeker", "employer", "admin"):
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(base_url + "/login")
        page.fill("input[name=email]", f"{role}@example.test")
        page.fill("input[name=password]", PASSWORD)
        page.click("form.auth__form button[type=submit]")
        page.wait_for_load_state("networkidle")
        states[role] = ctx.storage_state()
        ctx.close()
    return states


@pytest.fixture
def open_page(browser, base_url, sessions):
    contexts = []

    def _open(path, width=360, scheme="light", role="guest"):
        ctx = browser.new_context(
            viewport={"width": width, "height": 800}, color_scheme=scheme, storage_state=sessions[role],
            is_mobile=width < 768, has_touch=width < 768,
        )
        ctx.add_init_script('localStorage.setItem("rabota_consent", JSON.stringify({v:1,analytics:false,at:"2026-01-01"}))')
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(base_url + path, wait_until="load")
        return page

    yield _open
    for c in contexts:
        c.close()


SMALL_TARGETS_JS = """
() => {
  const bad = [];
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button]';
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('.hp, .skip-link, .visually-hidden')) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    // карточка вакансии нажимается целиком (растянутая ссылка), строка внутри абзаца не считается целью
    if (el.matches('.vcard__link')) continue;
    if (el.matches('a') && cs.display === 'inline' && el.closest('p, li, dd, td, label, small')) continue;
    if (el.matches('[type=checkbox], [type=radio]') && el.closest('label')) continue;
    if (el.closest('[inert], .filters:not(.is-open), .umenu:not([open]) .umenu__panel, .mmenu:not(.is-open)')) continue;
    if (r.height < %(min)d - 0.5 || r.width < %(min)d - 0.5) {
      bad.push(el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0] + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' "' + (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 24) + '"');
    }
  }
  return bad;
}
""".replace("%(min)d", str(MIN_TAP))

PUBLIC_PAGES = ["/", "/vacancies", "/vacancies?q=ъъъъ", "/profession/%D0%B1%D0%B0%D1%80%D0%B8%D1%81%D1%82%D0%B0", "/employers", "/razmestit",
                "/about", "/legal", "/legal/privacy", "/legal/request", "/login", "/register", "/forgot", "/nothing-here"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_touch_targets_public_pages_on_phone(open_page, path):
    page = open_page(path, 360)
    assert page.evaluate(SMALL_TARGETS_JS) == [], path


def test_touch_targets_vacancy_page_on_phone(open_page, db_file):
    c = sqlite3.connect(db_file)
    vid = c.execute("SELECT id FROM vacancies WHERE origin = 'direct' AND moderation_status = 'approved' ORDER BY id LIMIT 1").fetchone()[0]
    c.close()
    page = open_page(f"/vacancy/{vid}", 360)
    assert page.evaluate(SMALL_TARGETS_JS) == []


@pytest.mark.parametrize(
    ("path", "role"),
    [("/cabinet", "seeker"), ("/cabinet", "employer"), ("/account", "seeker"), ("/responses", "seeker"), ("/responses", "employer"),
     ("/responses/1", "seeker"), ("/responses/1", "employer"), ("/messages", "seeker"), ("/messages/1", "seeker"),
     ("/notifications", "seeker"), ("/vacancy/%(vid)s/apply", "seeker")],
)
def test_touch_targets_account_pages_on_phone(open_page, db_file, path, role):
    c = sqlite3.connect(db_file)
    vid = c.execute("SELECT id FROM vacancies WHERE origin = 'direct' AND moderation_status = 'approved' ORDER BY id LIMIT 1").fetchone()[0]
    c.close()
    page = open_page(path % {"vid": vid} if "%(vid)s" in path else path, 360, role=role)
    assert page.evaluate(SMALL_TARGETS_JS) == [], f"{role} {path}"


@pytest.mark.parametrize("path", ["/admin", "/admin/moderation", "/admin/complaints", "/admin/requests", "/admin/stoplist", "/admin/audit"])
def test_touch_targets_admin_pages_on_phone(open_page, path):
    page = open_page(path, 360, role="admin")
    assert page.evaluate(SMALL_TARGETS_JS) == [], path


@pytest.mark.parametrize("width", [360, 390])
def test_category_names_are_not_broken_inside_a_word(open_page, width):
    page = open_page("/", width)
    broken = page.evaluate(
        """() => [...document.querySelectorAll('.cat__name')].filter((el) => {
          if (/\\s/.test(el.textContent.trim())) return false;
          const line = parseFloat(getComputedStyle(el).lineHeight);
          return el.getBoundingClientRect().height > line * 1.5;
        }).map((el) => el.textContent.trim())"""
    )
    assert broken == []


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_search_placeholders_fit_their_field(open_page, width):
    page = open_page("/", width)
    clipped = page.evaluate(
        """async () => { await document.fonts.ready; return [...document.querySelectorAll('.sbar__input')].filter((el) => el.offsetParent !== null).filter((el) => {
          const cs = getComputedStyle(el);
          const ctx = document.createElement('canvas').getContext('2d');
          ctx.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
          const need = ctx.measureText(el.placeholder).width;
          // в поле со списком подсказок браузер отнимает место под стрелку списка
          const have = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - (el.list ? 28 : 0);
          return need > have;
        }).map((el) => el.placeholder); }"""
    )
    assert clipped == []


def test_headings_do_not_skip_levels_in_the_feed(open_page):
    page = open_page("/vacancies", 360)
    levels = page.evaluate("() => [...document.querySelectorAll('h1,h2,h3,h4')].filter((h) => h.checkVisibility({ visibilityProperty: true })).map((h) => Number(h.tagName[1]))")
    jumps = [(a, b) for a, b in itertools.pairwise(levels) if b - a > 1]
    assert jumps == []


def test_admin_moderation_id_column_does_not_wrap(open_page):
    page = open_page("/admin/moderation", 1280, role="admin")
    wrapped = page.evaluate(
        """() => [...document.querySelectorAll('td.mono')].filter((td) => {
          const range = document.createRange();
          range.selectNodeContents(td);
          return new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size > 1;
        }).map((td) => td.textContent.trim())"""
    )
    assert wrapped == []


def test_admin_vacancy_page_uses_russian_statuses(open_page, db_file):
    c = sqlite3.connect(db_file)
    vid = c.execute("SELECT id FROM vacancies WHERE moderation_status = 'pending' LIMIT 1").fetchone()[0]
    c.close()
    page = open_page(f"/admin/vacancies/{vid}", 1280, role="admin")
    text = page.inner_text("body")
    assert "hidden" not in text and "pending" not in text


def test_cabinet_has_no_placeholder_copy(open_page):
    for role in ("seeker", "employer"):
        page = open_page("/cabinet", 360, role=role)
        assert "в разработке" not in page.inner_text("body")


NAMELESS_JS = """
() => [...document.querySelectorAll('a[href], button, summary')].filter((el) => {
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' || el.closest('.hp, [hidden]')) return false;
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  return !((el.innerText || '').trim() || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title);
}).map((el) => el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0])
"""


@pytest.mark.parametrize("width", [360, 1280])
@pytest.mark.parametrize("path", ["/admin", "/admin/moderation", "/admin/complaints", "/admin/stoplist"])
def test_admin_controls_have_names_when_text_is_hidden(open_page, path, width):
    page = open_page(path, width, role="admin")
    assert page.evaluate(NAMELESS_JS) == [], f"{path} {width}"


@pytest.mark.parametrize("saved", ["dark", "light"])
def test_saved_theme_is_applied_before_first_paint(browser, base_url, saved):
    """Сохранённая тема ставится скриптом в шапке: к началу разбора страницы цвет уже верный, мерцания нет."""
    other = "light" if saved == "dark" else "dark"
    ctx = browser.new_context(color_scheme=other)
    ctx.add_init_script(f'localStorage.setItem("theme", "{saved}")')
    page = ctx.new_page()
    page.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => { window.__themeAtParse = document.documentElement.getAttribute('data-theme'); });"
    )
    page.goto(base_url + "/", wait_until="load")
    assert page.evaluate("window.__themeAtParse") == saved
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert (bg == "rgb(15, 23, 42)") == (saved == "dark")
    ctx.close()
