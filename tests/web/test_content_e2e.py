# ruff: noqa: F811
"""Сквозные тесты контента и установки в браузере: подсказки поиска, страницы без горизонтальной прокрутки,
подсказка «Установить», service worker и страница «нет сети». Запуск: .venv/bin/pytest -m e2e tests/web/test_content_e2e.py"""
import pytest

from tests.web.test_web_e2e import (  # noqa: F401  (фикстуры общие с остальными сквозными тестами)
    DESKTOP,
    MOBILE,
    WIDTHS,
    base_url,
    browser,
    db_file,
    make_page,
    page,
    visit,
)

pytestmark = pytest.mark.e2e

PATHS = ["/podborki", "/podborki/rabota-bez-opyta", "/zarplaty", "/blog", "/blog/kak-sostavit-rezyume", "/offline"]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("path", PATHS)
def test_content_pages_fit_the_screen(make_page, width, path):
    pg = make_page({"width": width, "height": 800}, mobile=width < 800)
    visit(pg, path)
    sizes = pg.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sizes[0] <= sizes[1], f"{path} при {width}px шире экрана: {sizes}"
    assert pg.locator("h1").count() == 1


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_content_pages_open_without_console_errors_in_both_themes(make_page, scheme):
    pg = make_page(MOBILE, scheme=scheme, mobile=True)
    for path in PATHS:
        visit(pg, path)
    assert [e for e in pg.errors if "service" not in e.lower()] == []


def test_suggest_dropdown_selects_with_keyboard(page):
    visit(page, "/")
    field = page.locator("#hero-q")
    field.click()
    page.wait_for_selector(".sug__item")  # на фокусе показываются популярные
    field.fill("")
    field.type("ба")
    page.wait_for_function("() => { const e = document.querySelector('.sug__item span'); return !!e && e.textContent.toLowerCase().includes('ба'); }")
    assert field.get_attribute("aria-expanded") == "true" and field.get_attribute("role") == "combobox"
    first = page.locator(".sug__item span").first.inner_text()
    assert "ба" in first.lower()
    field.press("ArrowDown")
    assert page.locator(".sug__item[aria-selected=true]").count() == 1
    field.press("Enter")
    assert field.input_value() == first
    assert page.locator(".sug").first.is_hidden()
    assert page.url.endswith("/")  # Enter выбрал подсказку, форма не ушла


def test_suggest_closes_on_escape_and_works_for_places(page):
    visit(page, "/")
    where = page.locator("#hero-where")
    where.click()
    page.wait_for_selector("#hero-where-sug .sug__item")
    where.press("Escape")
    assert page.locator("#hero-where-sug").is_hidden()
    where.type("Дон")
    page.wait_for_function("document.querySelectorAll('#hero-where-sug .sug__item').length > 0")
    assert "Донецк" in page.locator("#hero-where-sug").inner_text()


def test_search_still_submits_without_choosing_a_suggestion(page):
    visit(page, "/")
    page.locator("#hero-q").fill("кассир")
    page.locator("#hero-q").press("Enter")
    page.wait_for_url("**/vacancies?**")
    assert "q=" in page.url


def test_install_prompt_appears_from_second_visit_once_and_remembers_refusal(make_page):
    pg = make_page(MOBILE, mobile=True)
    fire = """() => { const e = new Event('beforeinstallprompt'); e.prompt = () => Promise.resolve();
        e.userChoice = Promise.resolve({outcome: 'dismissed'}); window.dispatchEvent(e); }"""
    visit(pg, "/")  # первый визит: подсказки нет
    pg.evaluate(fire)
    pg.wait_for_timeout(4400)
    assert pg.locator(".pwa-bar").count() == 0
    pg.evaluate("sessionStorage.clear()")  # новый сеанс = второй визит
    visit(pg, "/")
    pg.evaluate(fire)
    pg.wait_for_selector(".pwa-bar", timeout=8000)
    bar = pg.locator(".pwa-bar")
    box = bar.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= MOBILE["width"]
    assert "Установить" in bar.inner_text() and "Не сейчас" in bar.inner_text()
    pg.get_by_role("button", name="Не сейчас").click()
    assert pg.locator(".pwa-bar").count() == 0
    assert "dismissed" in pg.evaluate("localStorage.getItem('pwa-install')")
    pg.evaluate("sessionStorage.clear()")
    visit(pg, "/")
    pg.evaluate(fire)
    pg.wait_for_timeout(4400)
    assert pg.locator(".pwa-bar").count() == 0  # отказ запомнен


def test_install_prompt_is_not_shown_on_desktop_or_on_forms(make_page):
    desktop = make_page(DESKTOP)
    for _ in range(2):
        desktop.evaluate("sessionStorage.clear()") if desktop.url != "about:blank" else None
        visit(desktop, "/")
    desktop.evaluate("window.dispatchEvent(new Event('beforeinstallprompt'))")
    desktop.wait_for_timeout(4400)
    assert desktop.locator(".pwa-bar").count() == 0
    pg = make_page(MOBILE, mobile=True)
    visit(pg, "/")
    pg.evaluate("sessionStorage.clear()")
    visit(pg, "/login")
    pg.evaluate("window.dispatchEvent(new Event('beforeinstallprompt'))")
    pg.wait_for_timeout(4400)
    assert pg.locator(".pwa-bar").count() == 0


def test_service_worker_registers_precaches_shell_and_serves_offline_page(make_page):
    pg = make_page(DESKTOP)
    visit(pg, "/about")
    pg.wait_for_function("navigator.serviceWorker.ready.then(() => true)", timeout=15000)
    pg.wait_for_function("navigator.serviceWorker.controller !== null || true")
    keys = pg.evaluate("caches.keys()")
    assert any(k.startswith("rabota-shell-") for k in keys)
    cached = pg.evaluate("caches.open(" + repr(next(k for k in keys if k.startswith("rabota-shell-"))) + ").then(c => c.keys()).then(r => r.map(x => new URL(x.url).pathname))")
    assert "/offline" in cached and any(p.endswith("site.css") for p in cached)
    assert not any(p.startswith(("/admin", "/cabinet", "/account", "/vacancy/", "/vacancies")) for p in cached)
    # перезагрузка под управлением service worker, затем обрыв сети: переход показывает страницу «нет сети»
    pg.reload()
    pg.wait_for_function("navigator.serviceWorker.controller !== null", timeout=10000)
    pg.context.set_offline(True)
    try:
        pg.goto(pg.base + "/vacancies", wait_until="load")
        assert "Нет подключения к сети" in pg.inner_text("h1")
    finally:
        pg.context.set_offline(False)
