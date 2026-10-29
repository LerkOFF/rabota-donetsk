"""Окно согласия на аналитику в браузере: вид на всех ширинах и в обеих темах, поведение кнопок, счётчик только после согласия.

Номер счётчика выдуманный, запросы к серверу Метрики перехватываются и не уходят в сеть. Запуск: .venv/bin/pytest -m e2e tests/web/test_web_consent_e2e.py
"""
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from rabota.web.app import create_app

pytestmark = pytest.mark.e2e

WIDTHS = [360, 390, 768, 1280, 1920]
METRIKA_JS = "window.__ymLoaded = true;"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    db = Path(tmp_path_factory.mktemp("consent")) / "site.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("METRIKA_ID", "12345678")
        app = create_app(db, site_url="http://127.0.0.1")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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
def open_page(browser, base_url):
    contexts = []

    def _open(width=390, scheme="light", path="/"):
        ctx = browser.new_context(viewport={"width": width, "height": 800}, color_scheme=scheme, is_mobile=width < 768, has_touch=width < 768)
        contexts.append(ctx)
        page = ctx.new_page()
        page.metrika_requests = []
        page.on("request", lambda r: page.metrika_requests.append(r.url) if "yandex" in r.url else None)
        ctx.route("**/mc.yandex.ru/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body=METRIKA_JS))
        page.goto(base_url + path, wait_until="load")
        page.wait_for_selector("#cookie-consent:not([hidden])")
        return page

    yield _open
    for c in contexts:
        c.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", WIDTHS)
def test_banner_fits_every_width_and_has_big_buttons(open_page, width, scheme):
    page = open_page(width, scheme)
    box = page.locator("#cookie-consent").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width, box
    assert box["y"] >= 0 and box["y"] + box["height"] <= 800, box
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    for sel in ("[data-consent=accept]", "[data-consent=reject]"):
        b = page.locator(sel).bounding_box()
        assert b["height"] >= 44 and b["width"] >= 44, (sel, b)
    # текст читаем в обеих темах: цвет текста заметно отличается от фона окна
    contrast = page.evaluate(
        """() => {
          const el = document.getElementById('cookie-consent');
          const parse = (c) => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
          const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
          const l1 = lum(parse(getComputedStyle(el).backgroundColor)), l2 = lum(parse(getComputedStyle(el.querySelector('.consent__text')).color));
          return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        }"""
    )
    assert contrast >= 4.5


def test_counter_is_not_loaded_before_consent(open_page):
    page = open_page()
    page.wait_for_timeout(300)
    assert page.metrika_requests == []
    assert page.locator("#ym-tag").count() == 0


def test_accept_loads_counter_and_remembers_choice(open_page):
    page = open_page()
    page.click("[data-consent=accept]")
    page.wait_for_selector("#ym-tag", state="attached")
    assert any("mc.yandex.ru/metrika/tag.js" in u for u in page.metrika_requests)
    assert page.locator("#cookie-consent").is_hidden()
    page.reload()
    page.wait_for_selector("#ym-tag", state="attached")
    assert page.locator("#cookie-consent").is_hidden()


def test_reject_keeps_counter_off_and_banner_gone(open_page):
    page = open_page()
    page.click("[data-consent=reject]")
    page.wait_for_timeout(300)
    assert page.metrika_requests == []
    page.reload()
    page.wait_for_timeout(300)
    assert page.metrika_requests == []
    assert page.locator("#cookie-consent").is_hidden()


def test_choice_can_be_revoked_from_the_footer(open_page):
    page = open_page()
    page.click("[data-consent=accept]")
    page.wait_for_selector("#ym-tag", state="attached")
    page.click(".consent-link")
    assert page.locator("#cookie-consent").is_visible()
    assert page.evaluate("document.activeElement.getAttribute('data-consent')") == "accept"
    page.click("[data-consent=reject]")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)
    assert page.locator("#ym-tag").count() == 0
    assert page.evaluate("JSON.parse(localStorage.getItem('rabota_consent')).analytics") is False


def test_escape_closes_banner_without_choosing(open_page):
    page = open_page()
    page.keyboard.press("Escape")
    assert page.locator("#cookie-consent").is_hidden()
    assert page.metrika_requests == []
    assert page.evaluate("localStorage.getItem('rabota_consent')") is None


def test_focused_element_is_not_hidden_behind_the_banner(open_page):
    page = open_page(390, path="/vacancies")
    banner_top = page.locator("#cookie-consent").bounding_box()["y"]
    covered = []
    for _ in range(40):
        page.keyboard.press("Tab")
        box = page.evaluate(
            """() => { const el = document.activeElement; if (!el || el === document.body || el.closest('#cookie-consent')) return null;
              const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, text: (el.innerText || el.className || '').slice(0, 30) }; }"""
        )
        if box and box["bottom"] > banner_top and box["top"] < 800:
            covered.append(box["text"])
    assert covered == []
