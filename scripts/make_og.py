"""Рисует картинку для ссылок в соцсетях и мессенджерах: rabota/web/static/og.png (1200x630).

Запуск: .venv/bin/python scripts/make_og.py
Нужен Chromium из Playwright (он уже стоит для браузерных тестов). Шрифт Onest берётся из static/fonts.
Знак (портфель) совпадает с favicon.svg; при смене знака обновить и здесь.
"""
from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "rabota" / "web" / "static"

MARK = """
<svg viewBox="0 0 32 32" width="{size}" height="{size}">
  <rect width="32" height="32" rx="8" fill="{bg}" fill-opacity="{tile}"/>
  <path d="M11.6 11.4V9.8A2.6 2.6 0 0 1 14.2 7.2h3.6a2.6 2.6 0 0 1 2.6 2.6v1.6" fill="none" stroke="{fg}" stroke-width="2.4" stroke-linecap="round"/>
  <rect x="5.5" y="11" width="21" height="14.5" rx="3.4" fill="{fg}"/>
  <path d="M5.5 17.6h21" stroke="{bg}" stroke-width="1.5"/>
  <rect x="13.6" y="15.4" width="4.8" height="4.6" rx="1.3" fill="#ff9478"/>
</svg>"""

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
@font-face {{ font-family: "Onest"; font-weight: 400 800; src: url("data:font/woff2;base64,{font}") format("woff2"); }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ width: 1200px; height: 630px; overflow: hidden; position: relative; background: #1e3a9a;
  font-family: "Onest", sans-serif; color: #fff; }}
.bg-mark {{ position: absolute; right: -60px; bottom: -90px; opacity: .12; transform: rotate(-8deg); }}
.top {{ position: absolute; left: 80px; top: 44px; display: flex; align-items: center; gap: 22px; font-size: 34px; font-weight: 700; }}
h1 {{ position: absolute; left: 80px; top: 150px; width: 1080px; font-size: 92px; line-height: 1.04; font-weight: 800; letter-spacing: -1px; }}
.sub {{ position: absolute; left: 80px; top: 372px; font-size: 28px; color: #c7d6ff; }}
.chip {{ position: absolute; left: 80px; top: 446px; padding: 22px 38px; border-radius: 28px; background: #ff9478; color: #3a1300; }}
.chip b {{ display: block; font-size: 40px; font-weight: 800; }}
.chip span {{ font-size: 24px; font-weight: 500; }}
</style></head><body>
<div class="bg-mark">{big}</div>
<div class="top">{small}<span>Работа Донецк</span></div>
<h1>Работа в Донецке без лишних постов</h1>
<p class="sub">Оплата, район и телефон в одной карточке</p>
<div class="chip"><b>4 000 ₽ за смену</b><span>Бариста, Ворошиловский район</span></div>
</body></html>"""


def main() -> None:
    html = PAGE.format(
        font=base64.b64encode((STATIC / "fonts" / "onest-cyrillic.woff2").read_bytes()).decode(),
        big=MARK.format(size=620, bg="#1e3a9a", fg="#ffffff", tile=0),
        small=MARK.format(size=72, bg="#ffffff", fg="#2563eb", tile=1),
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        page.set_content(html)
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        page.screenshot(path=str(STATIC / "og.png"), type="png")
        browser.close()


if __name__ == "__main__":
    main()
