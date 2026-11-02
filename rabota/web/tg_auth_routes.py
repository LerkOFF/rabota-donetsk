"""Вход через Telegram и привязка Telegram в профиле.

Маршруты: POST /login/telegram (начать вход), GET /login/telegram (страница ожидания), GET /login/telegram/status
(опрос статуса), POST /login/telegram/complete (забрать вход и получить сессию); в профиле POST /account/telegram/link,
GET /account/telegram/status, POST /account/telegram/unlink.

Аккаунт через Telegram не создаётся: войти можно только в аккаунт, к которому Telegram уже привязан в профиле.
Браузер, начавший вход, получает случайное значение в cookie: без него подтверждённый вход забрать нельзя.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.accounts import service
from rabota.accounts import telegram as tg
from rabota.accounts.deps import (
    SESSION_COOKIE,
    Accounts,
    check_csrf,
    client_ip,
    get_current_user,
    is_https,
    rotate_csrf,
    safe_next,
    set_session_cookie,
)
from rabota.accounts.forms import read_form
from rabota.web import auth_routes as auth

router = APIRouter()

NONCE_COOKIE = "rabota_tglogin"
NONCE_PATH = "/login/telegram"

UNAVAILABLE_TEXT = "Вход через Telegram сейчас недоступен. Войдите по почте и паролю."
LINK_UNAVAILABLE_TEXT = "Привязка Telegram сейчас недоступна. Попробуйте позже."
LIMIT_TEXT = "Слишком много попыток. Подождите несколько минут и попробуйте снова."

# Состояния страницы входа: заголовок, пояснение, значок
LOGIN_STATES: dict[str, dict[str, str]] = {
    "waiting": {"heading": "Подтвердите вход в Telegram", "icon": "telegram-logo"},
    "confirmed": {"heading": "Вход подтверждён", "icon": "check-circle"},
    "unlinked": {"heading": "Telegram не привязан к аккаунту", "icon": "warning"},
    "blocked": {"heading": "Вход недоступен", "icon": "warning"},
    "expired": {"heading": "Время вышло", "icon": "clock"},
    "unavailable": {"heading": "Вход через Telegram недоступен", "icon": "warning"},
}


def _acc(request: Request) -> Accounts:
    return auth._acc(request)


def login_button(acc: Accounts) -> bool:
    """Показывать ли на /login кнопку: бот настроен и включён в настройках."""
    conn = acc.db()
    try:
        return tg.available(conn)
    finally:
        conn.close()


def _nonce(request: Request) -> str:
    return request.cookies.get(NONCE_COOKIE, "")[:120]


def _set_nonce(response: Response, request: Request, nonce: str) -> None:
    response.set_cookie(
        NONCE_COOKIE, nonce, max_age=int(tg.LOGIN_TTL.total_seconds()) + 60, path=NONCE_PATH, httponly=True,
        samesite="lax", secure=is_https(request),
    )


def _clear_nonce(response: Response, request: Request) -> None:
    response.delete_cookie(NONCE_COOKIE, path=NONCE_PATH, httponly=True, samesite="lax", secure=is_https(request))


def _login_page(
    request: Request, state: str, *, link: str = "", nxt: str = "", status: int = 200,
) -> Response:
    meta = LOGIN_STATES[state]
    return auth.render(request, "auth/tg_login.html", {
        "state": state, "heading": meta["heading"], "icon_name": meta["icon"], "tg_link": link, "next": nxt,
        "ttl_minutes": int(tg.LOGIN_TTL.total_seconds() // 60), "blocked_text": service.BLOCKED_TEXT,
        "unavailable_text": UNAVAILABLE_TEXT,
        "seo": {"title": "Вход через Telegram: Работа Донецк", "description": "Вход в аккаунт на сайте Работа Донецк через Telegram."},
    }, status)


def _with_next(path: str, nxt: str) -> str:
    return f"{path}?next={quote(nxt, safe='/')}" if nxt else path


@router.get("/login/telegram")
async def login_telegram_page(request: Request) -> Response:
    nxt = safe_next(request.query_params.get("next"), "")
    if get_current_user(request):
        return auth._redirect(nxt or "/cabinet")
    acc = _acc(request)
    nonce = _nonce(request)

    def work() -> tuple[bool, str, str]:
        conn = acc.db()
        try:
            if not tg.available(conn):
                return False, "unavailable", ""
            state = tg.login_state(conn, acc.secret, nonce, acc.now()) if nonce else "none"
            code = tg.code_for_nonce(acc.secret, nonce) if state not in ("none", "expired") else ""
            return True, state, code
        finally:
            conn.close()

    ok, state, code = await run_in_threadpool(work)
    if not ok:
        return _login_page(request, "unavailable", nxt=nxt, status=503)
    if state == "none":
        return auth._redirect(_with_next("/login", nxt))
    link = tg.config().start_link(tg.LOGIN_PREFIX + code) if code else ""
    return _login_page(request, state, link=link, nxt=nxt, status=410 if state == "expired" else 200)


@router.post("/login/telegram")
async def login_telegram_start(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    nxt = safe_next(form.get("next"), "")
    if get_current_user(request):
        return auth._redirect(nxt or "/cabinet")
    ip = client_ip(request)
    if not acc.limiter.allow("tg_login_start_ip", ip):
        raise HTTPException(429, LIMIT_TEXT)

    def work() -> tuple[str, str] | None:
        conn = acc.db()
        try:
            if not tg.available(conn):
                return None
            return tg.start_login(conn, acc.secret, ip, acc.now())
        finally:
            conn.close()

    started = await run_in_threadpool(work)
    if started is None:
        return _login_page(request, "unavailable", nxt=nxt, status=503)
    nonce, _code = started
    response = auth._redirect(_with_next("/login/telegram", nxt))
    _set_nonce(response, request, nonce)
    return response


@router.get("/login/telegram/status")
async def login_telegram_status(request: Request) -> Response:
    acc = _acc(request)
    if not acc.limiter.allow("tg_login_poll_ip", client_ip(request)):
        raise HTTPException(429, LIMIT_TEXT)
    nonce = _nonce(request)

    def work() -> str:
        conn = acc.db()
        try:
            if not tg.available(conn):
                return "unavailable"
            return tg.login_state(conn, acc.secret, nonce, acc.now()) if nonce else "none"
        finally:
            conn.close()

    state = await run_in_threadpool(work)
    response = JSONResponse({"state": state})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/login/telegram/complete")
async def login_telegram_complete(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    nxt = safe_next(form.get("next"), "")
    ip = client_ip(request)
    nonce = _nonce(request)
    if acc.limiter.blocked("tg_login_fail_ip", ip):
        raise HTTPException(429, LIMIT_TEXT)
    old_session = request.cookies.get(SESSION_COOKIE, "")

    def work() -> tuple[str, str, datetime | None, str]:
        conn = acc.db()
        try:
            if not tg.available(conn):
                return "unavailable", "", None, ""
            status, user = tg.complete_login(conn, acc.secret, nonce, acc.now()) if nonce else ("none", None)
            if status != "ok" or user is None:
                code = tg.code_for_nonce(acc.secret, nonce) if nonce and status in ("waiting", "unlinked", "blocked") else ""
                return status, "", None, code
            now = acc.now()
            raw = service.create_session(conn, user["id"], now, ip, request.headers.get("user-agent", ""))
            if old_session:
                service.delete_session(conn, old_session)
            return "ok", raw, now + service.SESSION_TTL, ""
        finally:
            conn.close()

    status, raw, expires, code = await run_in_threadpool(work)
    if status == "ok" and expires is not None:
        response = auth._redirect(nxt or "/cabinet")
        set_session_cookie(response, request, raw, expires)
        _clear_nonce(response, request)
        rotate_csrf(request)
        request.state.session_touched = True
        return response
    if status in ("none", "expired", "blocked"):
        acc.limiter.record("tg_login_fail_ip", ip)
    if status == "none":
        return auth._redirect(_with_next("/login", nxt))
    state = {"waiting": "waiting", "unlinked": "unlinked", "blocked": "blocked", "unavailable": "unavailable"}.get(
        status, "expired"
    )
    link = tg.config().start_link(tg.LOGIN_PREFIX + code) if code else ""
    code_status = {"waiting": 200, "unlinked": 200, "blocked": 403, "unavailable": 503}.get(status, 410)
    page = _login_page(request, state, link=link, nxt=nxt, status=code_status)
    if state in ("expired", "blocked"):
        _clear_nonce(page, request)
    return page


# ------------------------------------------------------------------ профиль

def account_context(acc: Accounts, user: dict[str, Any], link_code: str = "") -> dict[str, Any]:
    """Данные блока Telegram на странице /account."""
    conn = acc.db()
    try:
        available = tg.available(conn)
        info = tg.link_info(conn, user["id"])
    finally:
        conn.close()
    cfg = tg.config()
    return {
        **info, "available": available, "bot_username": cfg.bot_username,
        "link_url": cfg.start_link(tg.LINK_PREFIX + link_code) if link_code and available else "",
        "ttl_minutes": int(tg.LINK_TTL.total_seconds() // 60),
    }


@router.post("/account/telegram/link")
async def account_telegram_link(request: Request, user: auth.Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("tg_link_user", str(user["id"])):
        raise HTTPException(429, LIMIT_TEXT)

    def work() -> tuple[str, str]:
        conn = acc.db()
        try:
            if not tg.available(conn):
                return "unavailable", ""
            if tg.link_info(conn, user["id"])["linked"]:
                return "linked", ""
            return "ok", tg.issue_link_code(conn, user["id"], acc.now())
        finally:
            conn.close()

    status, code = await run_in_threadpool(work)
    if status == "unavailable":
        return auth._account_page(request, user, page_error=LINK_UNAVAILABLE_TEXT, status=503)
    if status == "linked":
        return auth._redirect("/account")
    return auth._account_page(request, user, tg_link_code=code)


@router.get("/account/telegram/status")
async def account_telegram_status(request: Request, user: auth.Verified) -> Response:
    acc = _acc(request)
    if not acc.limiter.allow("tg_login_poll_ip", client_ip(request)):
        raise HTTPException(429, LIMIT_TEXT)

    def work() -> bool:
        conn = acc.db()
        try:
            return bool(tg.link_info(conn, user["id"])["linked"])
        finally:
            conn.close()

    response = JSONResponse({"linked": await run_in_threadpool(work)})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/account/telegram/unlink")
async def account_telegram_unlink(request: Request, user: auth.Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("tg_unlink_user", str(user["id"])):
        raise HTTPException(429, LIMIT_TEXT)
    if form.get("confirm") != "1":
        return auth._account_page(request, user, errors={"telegram": {"confirm": "Подтвердите, что хотите отвязать Telegram."}}, status=422)

    def work() -> str:
        conn = acc.db()
        try:
            return tg.unlink(conn, user["id"], acc.now())
        finally:
            conn.close()

    status = await run_in_threadpool(work)
    if status == "no_login":
        return auth._account_page(
            request, user, status=409,
            page_error="Telegram отвязать нельзя: без него вы не сможете войти. Сначала задайте пароль и подтвердите почту.",
        )
    return auth._redirect("/account?saved=tg_unlinked")
