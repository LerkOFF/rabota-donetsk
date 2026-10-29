"""Заголовки безопасности и кеш статики.

Одно место для всех ответов сайта: CSP, Referrer-Policy, Permissions-Policy, nosniff, запрет встраивания,
HSTS (только по https) и правила кеша /static. nginx в боевой схеме сам выставляет HSTS и nosniff и убирает
их копии из ответа приложения, остальное приходит отсюда.

HSTS без includeSubDomains и preload: сначала проверяем на живом сайте, потом решаем про расширение.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI

HSTS = "max-age=31536000"
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "payment=(), usb=(), interest-cohort=(), browsing-topics=()"
)
REFERRER_POLICY = "strict-origin-when-cross-origin"
STATIC_VERSIONED = "public, max-age=31536000, immutable"
STATIC_PLAIN = "public, max-age=604800"

METRIKA_HOSTS = ("https://mc.yandex.ru", "https://mc.yandex.com")
METRIKA_STATIC = "https://yastatic.net"

BASE_CSP = {
    "default-src": ["'self'"],
    "img-src": ["'self'", "data:"],
    "style-src": ["'self'"],
    "script-src": ["'self'"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
    "base-uri": ["'self'"],
    "object-src": ["'none'"],
}


def build_csp(metrika: bool = False, upgrade: bool = False) -> str:
    """Политика CSP. С метрикой разрешаются её адреса для скрипта, картинок-маяков, соединений и вебвизора."""
    policy = {k: list(v) for k, v in BASE_CSP.items()}
    if metrika:
        policy["script-src"] += [*METRIKA_HOSTS, METRIKA_STATIC]
        policy["img-src"] += list(METRIKA_HOSTS)
        policy["connect-src"] += [*METRIKA_HOSTS, *(h.replace("https://", "wss://") for h in METRIKA_HOSTS)]
        policy["frame-src"] = list(METRIKA_HOSTS)
        policy["child-src"] = ["blob:", *METRIKA_HOSTS]
        policy["worker-src"] = ["blob:"]
    parts = [f"{name} {' '.join(values)}" for name, values in policy.items()]
    if upgrade:
        parts.append("upgrade-insecure-requests")
    return "; ".join(parts)


def _is_https(scope: dict[str, Any], headers: dict[bytes, bytes]) -> bool:
    if scope.get("scheme") == "https":
        return True
    return headers.get(b"x-forwarded-proto", b"").split(b",")[0].strip().lower() == b"https"


def static_cache_control(path: str, query: bytes) -> str | None:
    """Файл с ?v=<версия> в адресе не меняется под этим адресом: год и immutable. Без версии: неделя."""
    if not path.startswith("/static/"):
        return None
    versioned = "v" in parse_qs(query.decode("latin-1"), keep_blank_values=False)
    return STATIC_VERSIONED if versioned else STATIC_PLAIN


class SecurityHeadersMiddleware:
    """ASGI-прослойка. Заголовок, уже выставленный обработчиком, не перезаписывается (кроме кеша статики)."""

    def __init__(self, app: Callable[..., Any], *, site_https: bool = True, metrika: bool = False) -> None:
        self.app = app
        self.site_https = site_https
        self.csp = build_csp(metrika=metrika, upgrade=site_https)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req_headers = dict(scope.get("headers") or [])
        https = _is_https(scope, req_headers)
        cache = static_cache_control(scope.get("path", ""), scope.get("query_string", b""))

        async def wrapped(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}

                def add(name: str, value: str) -> None:
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))

                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("Referrer-Policy", REFERRER_POLICY)
                add("Content-Security-Policy", self.csp)
                add("Permissions-Policy", PERMISSIONS_POLICY)
                add("Cross-Origin-Opener-Policy", "same-origin")
                if https and self.site_https:
                    add("Strict-Transport-Security", HSTS)
                if cache is not None and message.get("status") in (200, 304):
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", cache.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


def install_security(app: FastAPI, *, site_url: str, metrika: bool = False) -> None:
    app.add_middleware(SecurityHeadersMiddleware, site_https=site_url.startswith("https://"), metrika=metrika)
