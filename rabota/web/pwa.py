"""Установка на телефон: manifest.webmanifest, service worker и значки.

Service worker выдаётся с корня сайта (/sw.js), иначе он не сможет управлять всеми страницами. Шаблон лежит в
templates/sw.js, сервер подставляет версию (по времени правки файлов оболочки), список предзагрузки и список закрытых
путей из `seo.PRIVATE_PREFIXES`: кабинет, админка, формы и всё закрытое в кэш не попадают.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rabota.web import seo as seolib

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SW_TEMPLATE = BASE_DIR / "templates" / "sw.js"

THEME_COLOR = "#2563eb"
BACKGROUND_COLOR = "#f6f8fc"
APP_NAME = "Работа Донецк"
SHORT_NAME = "Работа"

# файлы оболочки: открываются на каждой странице и нужны офлайн-странице
SHELL_FILES = ("tokens.css", "site.css", "theme-init.js", "app.js", "pwa.js", "suggest.js", "favicon.svg", "icons.svg")
# закрытое сверх seo.PRIVATE_PREFIXES: формы размещения, подсказки, сам service worker
EXTRA_NEVER = ("/razmestit", "/suggest", "/sw.js")

ICONS = (
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
)


def manifest() -> dict[str, Any]:
    return {
        "id": "/",
        "name": APP_NAME,
        "short_name": SHORT_NAME,
        "description": "Вакансии Донецка и ДНР: оплата, график, район и телефон.",
        "lang": "ru",
        "dir": "ltr",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "categories": ["business", "jobs"],
        "icons": list(ICONS),
        "shortcuts": [
            {"name": "Все вакансии", "short_name": "Вакансии", "url": "/vacancies?source=pwa",
             "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"}]},
            {"name": "Подработка на сегодня", "short_name": "Подработка", "url": "/podborki/podrabotka-na-segodnya?source=pwa",
             "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"}]},
            {"name": "Зарплаты", "short_name": "Зарплаты", "url": "/zarplaty?source=pwa",
             "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"}]},
        ],
    }


def never_cached_paths() -> list[str]:
    """Пути, которые service worker не кэширует и не отдаёт из кэша."""
    return sorted({*seolib.PRIVATE_PREFIXES, *EXTRA_NEVER})


def _mtime(name: str) -> int:
    try:
        return int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return 0


def shell_version() -> str:
    """Версия кэша: меняется при правке любого файла оболочки, старый кэш удаляется при активации."""
    stamp = max([_mtime(n) for n in SHELL_FILES] + [_mtime("icons/icon-192.png")] + [int(SW_TEMPLATE.stat().st_mtime)])
    return str(stamp)


def precache_urls() -> list[str]:
    urls = ["/offline"]
    for name in SHELL_FILES:
        if (STATIC_DIR / name).exists():
            urls.append(f"/static/{name}?v={_mtime(name)}")
    urls.append("/static/fonts/onest-cyrillic.woff2")
    urls.append("/static/icons/icon-192.png")
    return urls


def render_sw() -> str:
    """Текст service worker с подставленными значениями."""
    text = SW_TEMPLATE.read_text(encoding="utf-8")
    return (text.replace("__VERSION__", shell_version())
            .replace("__PRECACHE__", json.dumps(precache_urls(), ensure_ascii=False))
            .replace("__NEVER__", json.dumps(never_cached_paths(), ensure_ascii=False)))
