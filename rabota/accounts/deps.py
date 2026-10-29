"""Что нужно остальным частям сайта: текущий пользователь, зависимости доступа, CSRF, безопасный редирект.

Подключение: rabota.web.auth_routes.install(app). После него в каждом запросе есть
request.state.user (словарь пользователя или None для гостя) и request.state.csrf_token
(значение для скрытого поля csrf_token в любой POST-форме).

    from fastapi import Depends
    from rabota.accounts.deps import require_role

    @app.get("/cabinet/vacancies")
    def my_vacancies(user: dict = Depends(require_role("employer"))): ...

Словарь пользователя: id, email, role, name, phone, email_verified_at, status, consent_at, created_at,
last_login_at, verified, is_admin, role_label. Хеша пароля в нём нет.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from rabota.accounts import csrf, service
from rabota.accounts.mailer import Mailer
from rabota.accounts.ratelimit import RateLimiter

SESSION_COOKIE = "rabota_session"
CSRF_COOKIE = csrf.CSRF_COOKIE
DEFAULT_AFTER_LOGIN = "/cabinet"


@dataclass
class Accounts:
    """Настройки подсистемы аккаунтов, лежат в app.state.accounts."""

    db_path: Path | str
    mailer: Mailer
    limiter: RateLimiter
    secret: bytes
    site_url: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    trust_proxy: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def now(self) -> datetime:
        return self.clock()

    def db(self) -> sqlite3.Connection:
        return service.open_db(self.db_path)


class LoginRequired(Exception):
    """Нужен вход: обработчик отправляет на /login?next=..."""

    def __init__(self, next_url: str = ""):
        self.next_url = next_url


def accounts_of(request: Request) -> Accounts | None:
    return getattr(request.app.state, "accounts", None)


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def client_ip(request: Request) -> str:
    acc = accounts_of(request)
    if acc is not None and acc.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ текущий пользователь

def get_current_user(request: Request) -> dict[str, Any] | None:
    """Вошедший пользователь или None. Работает и без подключённого middleware (смотрит cookie сам)."""
    if hasattr(request.state, "user"):
        return request.state.user  # type: ignore[no-any-return]
    acc = accounts_of(request)
    raw = request.cookies.get(SESSION_COOKIE)
    user = None
    if acc is not None and raw:
        conn = acc.db()
        try:
            user, _ = service.resolve_session(conn, raw, acc.now())
        finally:
            conn.close()
    request.state.user = user
    return user


def login_url(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    if request.method not in ("GET", "HEAD") or target.startswith(("/login", "/logout")):
        return "/login"
    return "/login?next=" + quote(target, safe="/")


def require_user(request: Request) -> dict[str, Any]:
    """Вошедший пользователь. Гость уходит на /login?next=<эта страница>."""
    user = get_current_user(request)
    if user is None:
        raise LoginRequired(login_url(request))
    return user


def require_verified(request: Request) -> dict[str, Any]:
    """Вошедший пользователь с подтверждённой почтой. Без входа - на /login, без подтверждения - 403."""
    user = require_user(request)
    if not user.get("verified"):
        raise HTTPException(403, "Подтвердите почту, чтобы пользоваться этой страницей.")
    return user


def require_role(*roles: str) -> Callable[[Request], dict[str, Any]]:
    """Зависимость: нужна одна из ролей (job_seeker, employer, admin), иначе 403. Почта должна быть подтверждена."""
    if not roles:
        raise ValueError("require_role: укажите хотя бы одну роль")
    allowed = frozenset(roles)

    def dependency(request: Request) -> dict[str, Any]:
        user = require_verified(request)
        if user["role"] not in allowed:
            raise HTTPException(403, "Эта страница недоступна для вашей роли.")
        return user

    dependency.__name__ = "require_role_" + "_".join(sorted(allowed))
    return dependency


# ------------------------------------------------------------------ безопасный редирект

def safe_next(value: str | None, default: str = DEFAULT_AFTER_LOGIN) -> str:
    """Только внутренние пути вида /abc. Всё остальное (//host, схемы, обратные косые, переводы строк) - default."""
    if not value or len(value) > 500 or not value.startswith("/"):
        return default
    if value.startswith(("//", "/\\")) or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return default
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return default
    return value


# ------------------------------------------------------------------ CSRF

def csrf_token(request: Request) -> str:
    """Значение скрытого поля csrf_token для формы этого посетителя."""
    return getattr(request.state, "csrf_token", "")  # type: ignore[no-any-return]


def check_csrf(request: Request, form: dict[str, str]) -> None:
    """Проверка POST-формы: подпись токена по cookie и заголовок Origin. Иначе 403."""
    acc = accounts_of(request)
    if acc is None:
        raise HTTPException(403, "Запрос отклонён.")
    if not csrf.same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(403, "Запрос с другого сайта отклонён.")
    seed = request.cookies.get(CSRF_COOKIE)
    if not csrf.verify(seed, form.get(csrf.FORM_FIELD), acc.secret):
        raise HTTPException(403, "Форма устарела. Обновите страницу и отправьте её снова.")


def rotate_csrf(request: Request) -> None:
    """Выдать новое зерно CSRF (после входа и выхода)."""
    seed = csrf.new_seed()
    acc = accounts_of(request)
    request.state.csrf_seed = seed
    request.state.csrf_new = True
    if acc is not None:
        request.state.csrf_token = csrf.sign(seed, acc.secret)


# ------------------------------------------------------------------ cookie сессии

def set_session_cookie(response: Response, request: Request, raw: str, expires: datetime) -> None:
    acc = accounts_of(request)
    now = acc.now() if acc else datetime.now(UTC)
    max_age = max(int((expires - now).total_seconds()), 0)
    response.set_cookie(
        SESSION_COOKIE, raw, max_age=max_age, path="/", httponly=True, samesite="lax", secure=is_https(request)
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=is_https(request))


def session_expiry(now: datetime) -> datetime:
    return now + service.SESSION_TTL


__all__ = [
    "Accounts", "LoginRequired", "check_csrf", "client_ip", "csrf_token", "get_current_user", "is_https",
    "require_role", "require_user", "require_verified", "safe_next",
]
