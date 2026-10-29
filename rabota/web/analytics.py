"""Яндекс Метрика и подтверждение сайта в Вебмастере: подключаются только переменными окружения.

    METRIKA_ID               номер счётчика (цифры); пусто: ничего не подключается, баннера нет
    METRIKA_WEBVISOR         1 (по умолчанию) или 0: запись сессий; на страницах входа и кабинета выключена всегда
    WEBMASTER_VERIFY_FILE    имя файла подтверждения из Вебмастера, например yandex_0123abcd.html
    WEBMASTER_VERIFY_BODY    полное содержимое файла, если оно отличается от стандартного

Код счётчика загружается скриптом consent.js только после согласия посетителя (см. шаблон _analytics.html).
Сам шаблон читает настройки из request.app.state.analytics.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from rabota import config

VERIFY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,78}\.html$")

YANDEX_BODY = (
    "<html>\n    <head>\n        <meta http-equiv=\"Content-Type\" content=\"text/html; charset=UTF-8\">\n"
    "    </head>\n    <body>Verification: {code}</body>\n</html>\n"
)


@dataclass(frozen=True)
class Analytics:
    metrika_id: str = ""
    webvisor: bool = True
    verify_name: str = ""
    verify_body: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.metrika_id)


def verify_body_for(name: str, override: str = "") -> str:
    if override:
        return override
    m = re.match(r"^yandex_([0-9a-fA-F]+)\.html$", name)
    return YANDEX_BODY.format(code=m.group(1)) if m else ""


def from_env(env: Mapping[str, str] | None = None) -> Analytics:
    def val(name: str) -> str:
        return ((env.get(name) if env is not None else config.get(name)) or "").strip()

    mid = val("METRIKA_ID")
    name = val("WEBMASTER_VERIFY_FILE")
    if not VERIFY_RE.match(name):
        name = ""
    raw_visor = val("METRIKA_WEBVISOR").lower()
    return Analytics(
        metrika_id=mid if mid.isdigit() else "",
        webvisor=raw_visor not in {"0", "false", "no", "off"},
        verify_name=name,
        verify_body=verify_body_for(name, val("WEBMASTER_VERIFY_BODY")) if name else "",
    )


def install_analytics(app: FastAPI, settings: Analytics | None = None) -> Analytics:
    settings = settings or from_env()
    app.state.analytics = settings
    if settings.verify_name and settings.verify_body:
        body = settings.verify_body

        @app.get("/" + settings.verify_name, include_in_schema=False)
        def webmaster_verification() -> HTMLResponse:
            return HTMLResponse(body, headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})

    return settings
