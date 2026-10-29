"""Сквозные тесты в браузере (Chromium через Playwright). Сервер поднимается на свободном порту, база временная.

Запуск только их: .venv/bin/pytest -m e2e
"""
import importlib.util
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from rabota.web.app import create_app

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
MOBILE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 800}
WIDTHS = [360, 390, 768, 1280, 1920]


def load_seed():
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("e2e") / "demo.db"
    load_seed().seed(path)
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


@pytest.fixture
def make_page(browser, base_url):
    contexts = []

    def _make(viewport=None, scheme="light", mobile=False):
        ctx = browser.new_context(
            viewport=viewport or DESKTOP, color_scheme=scheme, is_mobile=mobile, has_touch=mobile,
            device_scale_factor=2 if mobile else 1,
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
        page.on("requestfailed", lambda r: page.errors.append(f"запрос не удался: {r.url}"))
        page.base = base_url
        return page

    yield _make
    for c in contexts:
        c.close()


@pytest.fixture(params=["mobile", "desktop"])
def page(request, make_page):
    if request.param == "mobile":
        return make_page(MOBILE, mobile=True)
    return make_page(DESKTOP)


def visit(page, path):
    page.goto(page.base + path, wait_until="load")
    return page


def barista_id(db_file):
    c = sqlite3.connect(db_file)
    row = c.execute("SELECT id FROM vacancies WHERE profession='бариста' AND duplicate_of IS NULL AND employer LIKE '%Зерно%'").fetchone()
    c.close()
    return row[0]


# ---------------------------------------------------------------- открываются


def test_home_opens(page):
    visit(page, "/")
    assert "Работа" in page.title()
    assert page.locator("h1").count() == 1
    assert page.locator("input[name=q]").is_visible()
    assert page.locator(".vcard").count() >= 6
    assert page.locator(".today__big").inner_text().strip().isdigit()
    assert page.errors == []


def test_fonts_are_loaded_from_site(page):
    visit(page, "/")
    page.evaluate("document.fonts.ready")
    assert page.evaluate("document.fonts.check('16px Onest', 'Работа')")


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("path", ["/", "/vacancies", "/vacancies?category=общепит", "/razmestit", "/about", "/nothing-here"])
def test_no_horizontal_scroll(make_page, width, path):
    page = make_page({"width": width, "height": 800}, mobile=width < 800)
    visit(page, path)
    sizes = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sizes[0] <= sizes[1], f"{path} при {width}px шире экрана: {sizes}"


@pytest.mark.parametrize("width", [360, 390, 1280])
def test_header_is_one_line(make_page, width):
    page = make_page({"width": width, "height": 800}, mobile=width < 800)
    visit(page, "/")
    assert page.locator(".site-header").bounding_box()["height"] <= 72
    assert page.locator(".logo").bounding_box()["height"] <= 40


@pytest.mark.parametrize("width", [360, 1280])
def test_no_horizontal_scroll_on_vacancy_page(make_page, db_file, width):
    page = make_page({"width": width, "height": 800}, mobile=width < 800)
    visit(page, f"/vacancy/{barista_id(db_file)}")
    sizes = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sizes[0] <= sizes[1]


# ---------------------------------------------------------------- поиск и фильтры


def test_search_from_home_with_declension(page):
    visit(page, "/")
    page.fill("input[name=q]", "кассира")
    page.press("input[name=q]", "Enter")
    page.wait_for_url("**/vacancies?q=*")
    titles = page.locator(".vcard__title").all_inner_texts()
    assert titles and all("Кассир" in t or "Продавец-кассир" in t for t in titles)


def test_filter_daily_pay(page):
    visit(page, "/vacancies")
    total = int(page.locator(".results-count").inner_text().split()[0])
    if page.locator("[data-filters-toggle]").is_visible():
        page.click("[data-filters-toggle]")
        assert page.locator("[data-filters-toggle]").get_attribute("aria-expanded") == "true"
    page.check("input[name=daily]")
    if page.locator("[data-filters-toggle]").is_visible():
        page.click(".filters__actions button[type=submit]")
    page.wait_for_url("**daily=1**")
    filtered = int(page.locator(".results-count").inner_text().split()[0])
    assert 0 < filtered < total
    cards = page.locator(".vcard")
    assert cards.count() == filtered or cards.count() == 20
    for i in range(min(cards.count(), 5)):
        assert "Выплата каждый день" in cards.nth(i).inner_text()
    assert page.locator(".chip--remove").count() >= 1


def test_filter_chip_removes_filter_and_select_filters(page):
    visit(page, "/vacancies?category=склад&daily=1")
    assert page.locator(".chip--remove").count() == 2
    page.locator(".chip--remove", has_text="Склад").click()
    page.wait_for_url("**/vacancies?daily=1")
    assert page.locator(".chip--remove").count() == 1


def test_no_results_state(page):
    visit(page, "/vacancies?q=яяяяя")
    assert page.get_by_text("Ничего не нашлось").is_visible()
    page.get_by_role("link", name="Сбросить фильтры").click()
    page.wait_for_url("**/vacancies")
    assert page.locator(".vcard").count() > 0


def test_pagination_show_more(page):
    visit(page, "/vacancies")
    page.get_by_role("link", name="Показать ещё").click()
    page.wait_for_url("**page=2")
    assert page.locator(".vcard").count() >= 10
    assert page.get_by_role("link", name="Назад").is_visible()


def test_military_and_duplicates_not_on_site(page):
    visit(page, "/vacancies?q=военн")
    assert page.locator(".vcard").count() == 0
    visit(page, "/vacancies?q=бариста")
    # главная карточка одна на каждое заведение, дубли не плодят карточки
    employers = page.locator(".vcard__employer").all_inner_texts()
    assert employers.count("Кофейня «Зерно»") == 1


# ---------------------------------------------------------------- карточка


def test_card_opens_and_has_actions(page, db_file):
    visit(page, "/vacancies?q=бариста")
    first = page.locator(".vcard", has_text="Кофейня «Зерно»").first
    assert first.get_by_role("link", name="Позвонить").get_attribute("href").startswith("tel:+7")
    assert first.get_by_role("link", name="Оригинал").get_attribute("target") == "_blank"
    assert "ещё в" in first.inner_text()
    first.locator(".vcard__link").click()
    page.wait_for_url("**/vacancy/*")
    assert page.locator("h1").inner_text() == "Бариста"
    assert page.get_by_text("4 000 ₽").first.is_visible()
    assert page.get_by_text("Эту вакансию также публикуют").is_visible()
    assert page.locator(".also-list__item").count() == 3
    assert page.locator(".also-list__link").first.get_attribute("href").startswith("http")
    assert page.errors == []


def test_whole_card_is_clickable(make_page, base_url):
    page = make_page(MOBILE, mobile=True)
    visit(page, "/vacancies")
    card = page.locator(".vcard").first
    box = card.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 60)
    page.wait_for_url("**/vacancy/*")


def test_vacancy_page_mobile_has_sticky_call_bar(make_page, db_file):
    page = make_page(MOBILE, mobile=True)
    visit(page, f"/vacancy/{barista_id(db_file)}")
    bar = page.locator(".action-bar")
    assert bar.is_visible()
    box = bar.bounding_box()
    assert abs(box["y"] + box["height"] - 844) < 2
    assert bar.get_by_role("link", name="Позвонить").is_visible()
    # контакты не прячутся в самом низу
    assert page.locator(".contact-box").is_visible()


def test_vacancy_page_desktop_has_contact_sidebar(make_page, db_file):
    page = make_page(DESKTOP)
    visit(page, f"/vacancy/{barista_id(db_file)}")
    assert not page.locator(".action-bar").is_visible()
    side = page.locator(".vpage__side")
    assert side.get_by_role("link", name="Позвонить").is_visible()
    box = side.bounding_box()
    assert box["x"] > 800


def test_complaint_form(page, db_file):
    vid = barista_id(db_file)
    visit(page, f"/vacancy/{vid}")
    page.get_by_text("Пожаловаться на вакансию").click()
    page.select_option("#c-reason", "Мошенничество или просьба заплатить за работу")
    page.fill("#c-comment", "Просят деньги за обучение")
    page.get_by_role("button", name="Отправить жалобу").click()
    page.wait_for_url("**complaint=sent")
    assert page.get_by_text("жалоба принята").is_visible()
    conn = sqlite3.connect(db_file)
    rows = conn.execute("SELECT reason FROM complaints WHERE vacancy_id=?", (vid,)).fetchall()
    conn.close()
    assert any("Просят деньги" in r[0] for r in rows)


def test_complaint_validation_error_is_shown(page, db_file):
    visit(page, f"/vacancy/{barista_id(db_file)}")
    page.get_by_text("Пожаловаться на вакансию").click()
    page.get_by_role("button", name="Отправить жалобу").click()
    assert page.get_by_text("Выберите причину из списка.").is_visible()


# ---------------------------------------------------------------- форма размещения


def test_post_form_errors_then_success(page, db_file):
    visit(page, "/razmestit")
    page.get_by_role("button", name="Отправить на проверку").click()
    summary = page.locator("#error-summary")
    assert summary.is_visible()
    assert page.evaluate("document.activeElement.id") == "error-summary"
    assert page.locator(".error").count() >= 3

    page.fill("#p-profession", "Курьер")
    page.select_option("#p-category", "транспорт")
    page.fill("#p-pay_min", "2500")
    page.select_option("#p-pay_period", "смена")
    page.fill("#p-phone", "+7 949 000-00-55")
    page.fill("#p-summary", "Развозить заказы по центру города на велосипеде.")
    page.get_by_role("button", name="Отправить на проверку").click()
    page.wait_for_url("**/razmestit/spasibo")
    assert page.get_by_text("Вакансия отправлена").is_visible()
    conn = sqlite3.connect(db_file)
    row = conn.execute("SELECT status, source_type FROM vacancies WHERE profession='курьер' AND source_type='site'").fetchone()
    conn.close()
    assert row == ("hidden", "site")


# ---------------------------------------------------------------- тема


def test_theme_toggle_switches_and_persists(page):
    visit(page, "/")
    bg_light = page.evaluate("getComputedStyle(document.body).backgroundColor")
    toggle = page.locator("[data-theme-toggle]")
    assert toggle.get_attribute("aria-pressed") == "false"
    toggle.click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    bg_dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg_dark != bg_light
    assert toggle.get_attribute("aria-pressed") == "true"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == bg_dark
    page.locator("[data-theme-toggle]").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == bg_light


def test_system_dark_scheme_is_respected(make_page):
    light = make_page(MOBILE, "light", mobile=True)
    dark = make_page(MOBILE, "dark", mobile=True)
    visit(light, "/")
    visit(dark, "/")
    assert light.evaluate("getComputedStyle(document.body).backgroundColor") != dark.evaluate("getComputedStyle(document.body).backgroundColor")
    # у переключателя в тёмной системной теме нажатие даёт светлую
    dark.locator("[data-theme-toggle]").click()
    assert dark.evaluate("document.documentElement.dataset.theme") == "light"


# ---------------------------------------------------------------- 404 и клавиатура


def test_404_page(page):
    response = page.goto(page.base + "/net-takoy-stranicy")
    assert response.status == 404
    assert page.get_by_role("heading", name="Страницы нет").is_visible()
    page.fill("input[name=q]", "повар")
    page.press("input[name=q]", "Enter")
    page.wait_for_url("**/vacancies?q=*")


def test_keyboard_skip_link_and_focus_ring(make_page):
    page = make_page(DESKTOP)
    visit(page, "/vacancies")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip-link"
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    ring = page.evaluate(
        "(() => { const s = getComputedStyle(document.activeElement);"
        " return [s.outlineStyle, s.outlineWidth, s.boxShadow]; })()"
    )
    # у кнопок и ссылок контур, у полей ввода кольцо из тени
    assert (ring[0] != "none" and ring[1] != "0px") or ring[2] != "none", ring


def test_tab_order_reaches_call_button(make_page):
    page = make_page(DESKTOP)
    visit(page, "/")
    seen = set()
    for _ in range(40):
        page.keyboard.press("Tab")
        seen.add(page.evaluate("document.activeElement.textContent.trim()"))
    assert "Позвонить" in seen


# ---------------------------------------------------------------- доступность

AUDIT_JS = """
() => {
  const problems = [];
  const name = (el) => (el.getAttribute('aria-label') || el.textContent || '').trim();
  if (!document.documentElement.lang) problems.push('нет lang');
  if (document.querySelectorAll('h1').length !== 1) problems.push('h1 должен быть один');
  let prev = 0;
  document.querySelectorAll('h1,h2,h3,h4').forEach((h) => {
    const n = Number(h.tagName[1]);
    if (prev && n > prev + 1) problems.push('пропущен уровень заголовка перед ' + h.textContent.trim());
    prev = n;
  });
  document.querySelectorAll('img').forEach((i) => { if (!i.hasAttribute('alt')) problems.push('img без alt ' + i.src); });
  document.querySelectorAll('input,select,textarea').forEach((f) => {
    if (f.type === 'hidden' || f.type === 'submit') return;
    if (f.closest('.hp')) return;
    const labelled = (f.labels && f.labels.length) || f.getAttribute('aria-label') || f.getAttribute('aria-labelledby');
    if (!labelled) problems.push('поле без подписи: ' + (f.name || f.id));
  });
  document.querySelectorAll('button').forEach((b) => { if (!name(b)) problems.push('кнопка без имени'); });
  document.querySelectorAll('a[href]').forEach((a) => { if (!name(a)) problems.push('ссылка без имени ' + a.href); });
  document.querySelectorAll('svg.icon').forEach((s) => { if (s.getAttribute('aria-hidden') !== 'true') problems.push('значок не скрыт от скринридера'); });
  document.querySelectorAll('[id]').forEach((el) => {
    if (document.querySelectorAll('[id="' + el.id + '"]').length > 1) problems.push('повтор id ' + el.id);
  });
  // контраст текста
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const over = (top, under) => ({ r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1 });
  const bgOf = (el) => { const stack = []; for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e); if (s.backgroundImage !== 'none') return null;
      const c = parse(s.backgroundColor); if (c && c.a > 0) { stack.push(c); if (c.a === 1) break; } }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base); return base; };
  const seen = new Set();
  document.querySelectorAll('body *').forEach((el) => {
    if (['SCRIPT', 'STYLE', 'OPTION', 'SELECT', 'INPUT', 'TEXTAREA', 'SVG', 'USE', 'PATH'].includes(el.tagName.toUpperCase())) return;
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own || el.closest('.hp, .visually-hidden, [hidden]')) return;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) return;
    const s = getComputedStyle(el); if (s.visibility === 'hidden' || s.opacity === '0') return;
    const fg = parse(s.color); const bg = bgOf(el); if (!fg || !bg) return;
    const f = over(fg, bg); const l1 = lum(f), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(s.fontSize); const bold = parseInt(s.fontWeight, 10) >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    const key = el.className + '|' + s.color + '|' + JSON.stringify(bg);
    if (ratio < need && !seen.has(key)) { seen.add(key); problems.push('контраст ' + ratio.toFixed(2) + ' < ' + need + ': ' + el.tagName + '.' + el.className + ' "' + el.textContent.trim().slice(0, 30) + '"'); }
  });
  return problems;
}
"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("path", ["/", "/vacancies", "/razmestit", "/about", "/nothing-here", "VACANCY"])
def test_basic_accessibility_audit(make_page, db_file, scheme, path):
    page = make_page(MOBILE, scheme, mobile=True)
    if path == "VACANCY":
        path = f"/vacancy/{barista_id(db_file)}"
    visit(page, path)
    problems = page.evaluate(AUDIT_JS)
    assert problems == [], f"{path} ({scheme}): {problems}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_accessibility_audit_with_open_forms(make_page, db_file, scheme):
    page = make_page(DESKTOP, scheme)
    visit(page, f"/vacancy/{barista_id(db_file)}")
    page.get_by_text("Пожаловаться на вакансию").click()
    assert page.evaluate(AUDIT_JS) == []
    visit(page, "/razmestit")
    page.get_by_role("button", name="Отправить на проверку").click()
    assert page.evaluate(AUDIT_JS) == []
