"""Маршруты аккаунтов и подключение к приложению.

    from rabota.web.auth_routes import install
    install(app, db_path=...)   # маршруты, middleware сессии, шаблоны

После install в каждом запросе есть request.state.user (словарь или None) и request.state.csrf_token.
Маршруты: /register, /login, POST /logout, /verify/<токен>, /verify/resend, /forgot, /reset/<токен>,
/account (+ /account/profile, /account/password, /account/email, /account/delete), /email/confirm/<токен>,
/cabinet (перенаправление по роли), /cabinet/seeker и /cabinet/employer (заглушки).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota import config
from rabota.accounts import csrf as csrf_mod
from rabota.accounts import service
from rabota.accounts.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Accounts,
    LoginRequired,
    accounts_of,
    check_csrf,
    clear_session_cookie,
    client_ip,
    csrf_token,
    get_current_user,
    is_https,
    require_role,
    require_verified,
    rotate_csrf,
    safe_next,
    set_session_cookie,
)
from rabota.accounts.forms import read_form
from rabota.accounts.mailer import Mail, Mailer, make_mailer, render_mail, send_safely
from rabota.accounts.ratelimit import RateLimiter
from rabota.accounts.service import (
    clean_name,
    normalize_email,
    normalize_phone,
    valid_email,
    validate_registration,
)
from rabota.accounts.tokens import peek_email_token
from rabota.web import format as fmt
from rabota.web import seo as seolib

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DEFAULT_SITE_URL = "https://работа-донецк.рф"

# Пути, которые браузеры и прокси не должны хранить в кэше
NO_STORE_PREFIXES = (
    "/login", "/logout", "/register", "/verify", "/forgot", "/reset", "/account", "/cabinet", "/email",
)

MAIL_FAIL_TEXT = "Не удалось отправить письмо. Попробуйте ещё раз чуть позже."
GENERIC_LOGIN_ERROR = "Неверная почта или пароль."

router = APIRouter()

Verified = Annotated[dict[str, Any], Depends(require_verified)]
Seeker = Annotated[dict[str, Any], Depends(require_role("job_seeker"))]
Employer = Annotated[dict[str, Any], Depends(require_role("employer"))]


# ------------------------------------------------------------------ вспомогательное

def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover - install не вызван
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


def _find_app_render(app: FastAPI) -> Callable[..., Response] | None:
    """Функция render основного приложения (она знает шапку, подсказки поиска и общий контекст), если её видно."""
    cached = getattr(app.state, "auth_app_render", False)
    if cached is not False:
        return cached  # type: ignore[no-any-return]
    found = None
    for route in app.router.routes:
        fn = getattr(route, "endpoint", None)
        code = getattr(fn, "__code__", None)
        if code is not None and fn.__closure__ and "render" in code.co_freevars:
            try:
                candidate = fn.__closure__[code.co_freevars.index("render")].cell_contents
            except ValueError:
                continue
            if callable(candidate):
                found = candidate
                break
    app.state.auth_app_render = found
    return found


def _header_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    """Пользователь для шапки так, как его готовит основное приложение (меню, подпись, инициал)."""
    try:
        from rabota.web.app import current_user
    except ImportError:  # pragma: no cover
        return user

    class _Req:
        class state:
            pass

    _Req.state.user = user
    return current_user(_Req)  # type: ignore[arg-type]


def render(request: Request, name: str, context: dict[str, Any] | None = None, status: int = 200) -> Response:
    """Страница аккаунта. В шаблонах пользователь называется me (user занят шапкой сайта)."""
    me = get_current_user(request)
    ctx: dict[str, Any] = {"me": me, "csrf_token": csrf_token(request), "seo": {}}
    ctx.update(context or {})
    ctx["seo"].setdefault("title", "Аккаунт: Работа Донецк")
    ctx["seo"].setdefault(
        "description",
        "Личный кабинет сайта Работа Донецк: вход, подтверждение почты, настройки аккаунта, отклики на вакансии "
        "и переписка с работодателями.")
    ctx["seo"]["noindex"] = True
    app_render = _find_app_render(request.app)
    if app_render is not None:
        return app_render(request, name, ctx, status)
    acc = _acc(request)
    templates: Jinja2Templates = request.app.state.auth_templates
    ctx.setdefault("path", request.url.path)
    ctx.setdefault("user", _header_user(me))
    ctx.setdefault("contact_email", config.get("CONTACT_EMAIL") or "")
    ctx["seo"] = seolib.finalize(ctx["seo"], acc.site_url, request.url.path)
    response = templates.TemplateResponse(request, name, ctx, status_code=status)
    seolib.apply_headers(response, ctx["seo"])
    return response


def notice(
    request: Request, heading: str, lines: list[str], actions: list[tuple[str, str, str]], *,
    status: int = 200, icon_name: str = "check-circle", warning: str = "", resend_email: str = "",
    mail: Mail | None = None,
) -> Response:
    """Страница-сообщение. mail: письмо, которое уйдёт уже после ответа (время ответа не выдаёт, была ли отправка)."""
    resp = render(request, "auth/notice.html", {
        "heading": heading, "lines": lines, "actions": actions, "icon_name": icon_name, "warning": warning,
        "resend_email": resend_email, "seo": {"title": heading},
    }, status)
    if mail is not None:
        resp.background = BackgroundTask(send_safely, _acc(request).mailer, mail)
    return resp


def _link(acc: Accounts, path: str, token: str) -> str:
    return f"{acc.site_url}{path}/{token}"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _token_gate(request: Request) -> None:
    """Перебор ссылок из писем: после череды неудач с одного адреса отвечаем 429."""
    if _acc(request).limiter.blocked("token_ip", client_ip(request)):
        raise HTTPException(429)


def _token_failed(request: Request) -> None:
    _acc(request).limiter.record("token_ip", client_ip(request))


def _bad_link(request: Request, what: str) -> Response:
    _token_failed(request)
    return notice(
        request, "Ссылка не подошла",
        [f"Ссылка {what} устарела или уже была использована. Запросите новую."],
        [("Запросить новую ссылку", "/verify/resend" if what == "подтверждения" else "/forgot", "primary"),
         ("Войти", "/login", "ghost")],
        status=400, icon_name="warning",
    )


# ------------------------------------------------------------------ регистрация

def _registered_page(request: Request, email: str, mail_failed: bool = False) -> Response:
    """Один и тот же ответ для нового и уже занятого адреса."""
    return notice(
        request, "Проверьте почту",
        [f"Мы отправили письмо на {email}. Откройте ссылку из письма, чтобы подтвердить адрес. Ссылка действует 24 часа.",
         "Если вы уже были зарегистрированы, в письме будет подсказка, как войти."],
        [("Ко входу", "/login", "ghost")],
        warning=MAIL_FAIL_TEXT if mail_failed else "", resend_email=email, icon_name="envelope-simple",
        status=200,
    )


@router.get("/register")
def register_form(request: Request) -> Response:
    user = get_current_user(request)
    if user:
        return _redirect("/cabinet")
    return render(request, "auth/register.html", {
        "form": {}, "errors": {},
        "seo": {"title": "Регистрация: Работа Донецк", "description": "Создайте аккаунт соискателя или работодателя."},
    })


@router.post("/register")
async def register_submit(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("register_ip", client_ip(request)):
        raise HTTPException(429)
    email = normalize_email(form.get("email", ""))
    if form.get("website"):  # ловушка для ботов: делаем вид, что приняли
        return _registered_page(request, email)
    name = clean_name(form.get("name", ""))
    password = form.get("password", "")
    role = form.get("role", "")
    consent = form.get("consent") == "1"
    errors = validate_registration(email, password, name, role, consent)
    if errors:
        shown = {"email": form.get("email", "").strip()[:254], "name": name, "role": role, "consent": consent}
        return render(request, "auth/register.html", {
            "form": shown, "errors": errors, "seo": {"title": "Регистрация: Работа Донецк"},
        }, 422)
    if not acc.limiter.allow("register_email", email):  # слишком много писем на один адрес: молча не шлём
        return _registered_page(request, email)

    def work() -> bool:
        conn = acc.db()
        try:
            reg = service.register(
                conn, email=email, password=password, name=name, role=role, consent=consent, now=acc.now()
            )
        finally:
            conn.close()
        if reg.token:
            mail = render_mail("verify", email, name=name, link=_link(acc, "/verify", reg.token), site_url=acc.site_url)
        else:
            mail = render_mail("already_registered", email, site_url=acc.site_url)
        return send_safely(acc.mailer, mail)

    sent = await run_in_threadpool(work)
    return _registered_page(request, email, mail_failed=not sent)


# ------------------------------------------------------------------ подтверждение почты

@router.get("/verify/resend")
def resend_form(request: Request) -> Response:
    return render(request, "auth/email_form.html", {
        "heading": "Отправить письмо ещё раз", "action": "/verify/resend", "button": "Отправить письмо",
        "lead": "Укажите почту, с которой вы регистрировались. Мы пришлём новую ссылку подтверждения.",
        "form": {"email": request.query_params.get("email", "")[:254]}, "error": "",
        "seo": {"title": "Повторное письмо подтверждения"},
    })


@router.post("/verify/resend")
async def resend_submit(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("resend_ip", client_ip(request)):
        raise HTTPException(429)
    email = normalize_email(form.get("email", ""))
    if not valid_email(email):
        return render(request, "auth/email_form.html", {
            "heading": "Отправить письмо ещё раз", "action": "/verify/resend", "button": "Отправить письмо",
            "lead": "Укажите почту, с которой вы регистрировались. Мы пришлём новую ссылку подтверждения.",
            "form": {"email": email}, "error": "Проверьте адрес почты.", "seo": {"title": "Повторное письмо"},
        }, 422)
    mail: Mail | None = None
    if acc.limiter.allow("resend_email", email):
        def work() -> Mail | None:
            conn = acc.db()
            try:
                found = service.resend_verification(conn, email, acc.now())
            finally:
                conn.close()
            if found is None:
                return None
            user, token = found
            return render_mail("verify", email, name=user["name"], link=_link(acc, "/verify", token), site_url=acc.site_url)

        mail = await run_in_threadpool(work)
    return notice(
        request, "Письмо отправлено",
        ["Если эта почта ждёт подтверждения, мы отправили на неё новую ссылку. Проверьте и папку со спамом."],
        [("Ко входу", "/login", "ghost")], icon_name="envelope-simple", mail=mail,
    )


@router.get("/verify/{token}")
async def verify(request: Request, token: str) -> Response:
    acc = _acc(request)
    _token_gate(request)

    def work() -> dict[str, Any] | None:
        conn = acc.db()
        try:
            return service.verify_email(conn, token, acc.now())
        finally:
            conn.close()

    user = await run_in_threadpool(work)
    if user is None:
        return _bad_link(request, "подтверждения")
    return notice(
        request, "Почта подтверждена",
        [f"Спасибо, {user['name']}! Теперь можно войти в аккаунт."],
        [("Войти", "/login", "primary"), ("Смотреть вакансии", "/vacancies", "ghost")],
    )


# ------------------------------------------------------------------ вход и выход

def _login_page(
    request: Request, *, email: str = "", error: str = "", nxt: str = "", unverified: bool = False, status: int = 200
) -> Response:
    return render(request, "auth/login.html", {
        "form": {"email": email}, "error": error, "next": nxt, "unverified": unverified, "info": "",
        "seo": {"title": "Вход: Работа Донецк", "description": "Вход в аккаунт на сайте Работа Донецк."},
    }, status)


@router.get("/login")
def login_form(request: Request) -> Response:
    nxt = safe_next(request.query_params.get("next"), "")
    if get_current_user(request):
        return _redirect(nxt or "/cabinet")
    return _login_page(request, nxt=nxt)


@router.post("/login")
async def login_submit(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    email = normalize_email(form.get("email", ""))
    password = form.get("password", "")
    nxt = safe_next(form.get("next"), "")
    ip = client_ip(request)
    if acc.limiter.blocked("login_ip", ip) or acc.limiter.blocked("login_email", email):
        raise HTTPException(429)
    old_session = request.cookies.get(SESSION_COOKIE, "")

    def work() -> tuple[service.AuthResult, str, datetime | None]:
        conn = acc.db()
        try:
            res = service.authenticate(conn, email, password)
            if res.status != "ok" or res.user is None:
                return res, "", None
            now = acc.now()
            raw = service.create_session(
                conn, res.user["id"], now, ip, request.headers.get("user-agent", "")
            )
            if old_session:
                service.delete_session(conn, old_session)
            return res, raw, now + service.SESSION_TTL
        finally:
            conn.close()

    res, raw, expires = await run_in_threadpool(work)
    if res.status == "bad":
        acc.limiter.record("login_ip", ip)
        acc.limiter.record("login_email", email)
        return _login_page(request, email=email, error=GENERIC_LOGIN_ERROR, nxt=nxt, status=401)
    if res.status == "blocked":
        return _login_page(request, email=email, error=service.BLOCKED_TEXT, nxt=nxt, status=403)
    if res.status == "unverified":
        return _login_page(
            request, email=email, nxt=nxt, unverified=True, status=403,
            error="Почта ещё не подтверждена. Откройте ссылку из письма, и вход заработает.",
        )
    acc.limiter.reset("login_email", email)
    response = _redirect(nxt or "/cabinet")
    assert expires is not None
    set_session_cookie(response, request, raw, expires)
    rotate_csrf(request)
    request.state.session_touched = True
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    raw = request.cookies.get(SESSION_COOKIE, "")

    def work() -> None:
        conn = acc.db()
        try:
            service.delete_session(conn, raw)
        finally:
            conn.close()

    await run_in_threadpool(work)
    response = _redirect("/")
    clear_session_cookie(response, request)
    rotate_csrf(request)
    request.state.session_touched = True
    return response


# ------------------------------------------------------------------ сброс пароля

@router.get("/forgot")
def forgot_form(request: Request) -> Response:
    return render(request, "auth/email_form.html", {
        "heading": "Сброс пароля", "action": "/forgot", "button": "Отправить ссылку",
        "lead": "Укажите почту аккаунта. Мы пришлём ссылку, по которой можно задать новый пароль.",
        "form": {}, "error": "", "seo": {"title": "Сброс пароля: Работа Донецк"},
    })


@router.post("/forgot")
async def forgot_submit(request: Request) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("forgot_ip", client_ip(request)):
        raise HTTPException(429)
    email = normalize_email(form.get("email", ""))
    if not valid_email(email):
        return render(request, "auth/email_form.html", {
            "heading": "Сброс пароля", "action": "/forgot", "button": "Отправить ссылку",
            "lead": "Укажите почту аккаунта. Мы пришлём ссылку, по которой можно задать новый пароль.",
            "form": {"email": email}, "error": "Проверьте адрес почты.", "seo": {"title": "Сброс пароля"},
        }, 422)
    mail: Mail | None = None
    if acc.limiter.allow("forgot_email", email):
        def work() -> Mail | None:
            conn = acc.db()
            try:
                found = service.request_reset(conn, email, acc.now())
            finally:
                conn.close()
            if found is None:
                return None
            user, token = found
            return render_mail("reset", email, name=user["name"], link=_link(acc, "/reset", token), site_url=acc.site_url)

        mail = await run_in_threadpool(work)
    return notice(
        request, "Проверьте почту",
        ["Если такая почта зарегистрирована, мы отправили на неё ссылку для сброса пароля. Она действует 1 час."],
        [("Ко входу", "/login", "ghost")], icon_name="envelope-simple", mail=mail,
    )


def _reset_page(request: Request, token: str, errors: dict[str, str] | None = None, status: int = 200) -> Response:
    return render(request, "auth/reset.html", {
        "token": token, "errors": errors or {}, "seo": {"title": "Новый пароль: Работа Донецк"},
    }, status)


@router.get("/reset/{token}")
async def reset_form(request: Request, token: str) -> Response:
    acc = _acc(request)
    _token_gate(request)

    def work() -> dict[str, Any] | None:
        conn = acc.db()
        try:
            return peek_email_token(conn, token, "reset", acc.now())
        finally:
            conn.close()

    if await run_in_threadpool(work) is None:
        return _bad_link(request, "для сброса пароля")
    return _reset_page(request, token)


@router.post("/reset/{token}")
async def reset_submit(request: Request, token: str) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    _token_gate(request)
    password, password2 = form.get("password", ""), form.get("password2", "")

    def work() -> tuple[str, dict[str, Any] | None]:
        conn = acc.db()
        try:
            tok = peek_email_token(conn, token, "reset", acc.now())
            if tok is None:
                return "bad_link", None
            owner = service.get_user(conn, tok["user_id"])
            problem = service.passwords.validate_password(password, owner["email"] if owner else "")
            if problem:
                return problem, None
            if password != password2:
                return "mismatch", None
            return "ok", service.reset_password(conn, token, password, acc.now())
        finally:
            conn.close()

    outcome, user = await run_in_threadpool(work)
    if outcome == "bad_link" or (outcome == "ok" and user is None):
        return _bad_link(request, "для сброса пароля")
    if outcome == "mismatch":
        return _reset_page(request, token, {"password2": "Пароли не совпадают."}, 422)
    if outcome != "ok" or user is None:
        return _reset_page(request, token, {"password": outcome}, 422)
    mail = render_mail("password_changed", user["email"], name=user["name"], site_url=acc.site_url)
    return notice(
        request, "Пароль изменён", ["Войдите с новым паролем. На всех устройствах вход закрыт."],
        [("Войти", "/login", "primary")], mail=mail,
    )


# ------------------------------------------------------------------ настройки аккаунта

FLASH = {
    "profile": "Профиль сохранён.",
    "password": "Пароль изменён. На других устройствах вход закрыт.",
    "email_sent": "Письмо отправлено на новый адрес. Почта изменится, когда вы откроете ссылку из письма.",
}


def _account_page(
    request: Request, user: dict[str, Any], errors: dict[str, dict[str, str]] | None = None,
    forms: dict[str, dict[str, str]] | None = None, flash: str = "", page_error: str = "", status: int = 200,
) -> Response:
    return render(request, "auth/account.html", {
        "me": user, "errors": errors or {}, "forms": forms or {}, "flash": flash, "page_error": page_error,
        "seo": {"title": "Настройки аккаунта: Работа Донецк"},
    }, status)


@router.get("/account")
def account(request: Request, user: Verified) -> Response:
    return _account_page(request, user, flash=FLASH.get(request.query_params.get("saved", ""), ""))


@router.post("/account/profile")
async def account_profile(request: Request, user: Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    name = clean_name(form.get("name", ""))
    phone = normalize_phone(form.get("phone", ""))
    errors: dict[str, str] = {}
    if not name:
        errors["name"] = "Укажите, как к вам обращаться."
    if phone is None:
        errors["phone"] = "Проверьте номер: например, +7 949 000-00-00."
    if errors:
        return _account_page(
            request, user, {"profile": errors}, {"profile": {"name": name, "phone": form.get("phone", "")[:30]}},
            status=422,
        )

    def work() -> None:
        conn = acc.db()
        try:
            service.update_profile(conn, user["id"], name, phone or "")
        finally:
            conn.close()

    await run_in_threadpool(work)
    return _redirect("/account?saved=profile")


@router.post("/account/password")
async def account_password(request: Request, user: Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("password_user", str(user["id"])):
        raise HTTPException(429)
    current, new, new2 = form.get("current_password", ""), form.get("password", ""), form.get("password2", "")
    session_raw = request.cookies.get(SESSION_COOKIE, "")

    def work() -> tuple[dict[str, str], Mail | None]:
        conn = acc.db()
        try:
            errors: dict[str, str] = {}
            if not service.check_password(conn, user["id"], current):
                errors["current_password"] = "Текущий пароль указан неверно."
            problem = service.passwords.validate_password(new, user["email"])
            if problem:
                errors["password"] = problem
            elif new != new2:
                errors["password2"] = "Пароли не совпадают."
            if errors:
                return errors, None
            service.change_password(conn, user["id"], new, acc.now(), keep_session=session_raw)
            return {}, render_mail("password_changed", user["email"], name=user["name"], site_url=acc.site_url)
        finally:
            conn.close()

    errors, mail = await run_in_threadpool(work)
    if errors:
        return _account_page(request, user, {"password": errors}, status=422)
    response = _redirect("/account?saved=password")
    if mail:
        response.background = BackgroundTask(send_safely, acc.mailer, mail)
    return response


@router.post("/account/email")
async def account_email(request: Request, user: Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if not acc.limiter.allow("change_email_user", str(user["id"])):
        raise HTTPException(429)
    new_email = normalize_email(form.get("new_email", ""))
    password = form.get("password", "")

    def work() -> tuple[dict[str, str], bool]:
        conn = acc.db()
        try:
            errors: dict[str, str] = {}
            if not valid_email(new_email):
                errors["new_email"] = "Проверьте адрес почты: он должен выглядеть как name@example.ru."
            elif new_email == user["email"]:
                errors["new_email"] = "Это ваша текущая почта."
            if not service.check_password(conn, user["id"], password):
                errors["password"] = "Пароль указан неверно."
            if errors:
                return errors, True
            token = service.request_email_change(conn, user["id"], new_email, acc.now())
            if token is None:  # адрес занят: хозяину адреса уходит предупреждение, а нам ответ такой же
                mail = render_mail("already_registered", new_email, site_url=acc.site_url)
            else:
                mail = render_mail(
                    "change_email", new_email, name=user["name"], link=_link(acc, "/email/confirm", token),
                    site_url=acc.site_url,
                )
            return {}, send_safely(acc.mailer, mail)
        finally:
            conn.close()

    errors, sent = await run_in_threadpool(work)
    if errors:
        return _account_page(request, user, {"email": errors}, {"email": {"new_email": form.get("new_email", "")[:254]}}, status=422)
    if not sent:
        return _account_page(request, user, page_error=MAIL_FAIL_TEXT, status=503)
    return _redirect("/account?saved=email_sent")


@router.get("/email/confirm/{token}")
async def email_confirm(request: Request, token: str) -> Response:
    acc = _acc(request)
    _token_gate(request)

    def work() -> tuple[dict[str, Any], str] | None:
        conn = acc.db()
        try:
            return service.confirm_email_change(conn, token, acc.now())
        finally:
            conn.close()

    result = await run_in_threadpool(work)
    if result is None:
        _token_failed(request)
        return notice(
            request, "Ссылка не подошла",
            ["Ссылка устарела, уже использована, или этот адрес успели занять. Запросите смену почты ещё раз."],
            [("Настройки аккаунта", "/account", "primary")], status=400, icon_name="warning",
        )
    user, old_email = result
    mail = render_mail("email_changed", old_email, name=user["name"], new_email=user["email"], site_url=acc.site_url)
    response = notice(
        request, "Почта изменена", [f"Теперь вход по адресу {user['email']}. Войдите заново."],
        [("Войти", "/login", "primary")], mail=mail,
    )
    clear_session_cookie(response, request)
    request.state.session_touched = True
    return response


@router.post("/account/delete")
async def account_delete(request: Request, user: Verified) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    check_csrf(request, form)
    if user["is_admin"]:
        raise HTTPException(403, "Аккаунт администратора через сайт удалить нельзя.")
    if not acc.limiter.allow("delete_user", str(user["id"])):
        raise HTTPException(429)
    errors: dict[str, str] = {}
    if form.get("confirm") != "1":
        errors["confirm"] = "Отметьте, что понимаете последствия."

    def work() -> bool:
        conn = acc.db()
        try:
            if not service.check_password(conn, user["id"], form.get("password", "")):
                return False
            if errors:
                return True
            service.delete_account(conn, user["id"])
            return True
        finally:
            conn.close()

    password_ok = await run_in_threadpool(work)
    if not password_ok:
        errors["password"] = "Пароль указан неверно."
    if errors:
        return _account_page(request, user, {"delete": errors}, status=422)
    response = _redirect("/account/deleted")
    clear_session_cookie(response, request)
    rotate_csrf(request)
    request.state.session_touched = True
    return response


@router.get("/account/deleted")
def account_deleted(request: Request) -> Response:
    return notice(
        request, "Аккаунт удалён", ["Мы удалили аккаунт и связанные с ним данные. Будем рады видеть вас снова."],
        [("На главную", "/", "primary"), ("Смотреть вакансии", "/vacancies", "ghost")],
    )


# ------------------------------------------------------------------ кабинет (заглушки)

@router.get("/cabinet")
def cabinet(request: Request, user: Verified) -> Response:
    target = {"job_seeker": "/cabinet/seeker", "employer": "/cabinet/employer"}.get(user["role"], "/account")
    return _redirect(target)


stub_router = APIRouter()


@stub_router.get("/cabinet/seeker")
def cabinet_seeker(request: Request, user: Seeker) -> Response:
    return render(request, "auth/cabinet_seeker.html", {"me": user, "seo": {"title": "Кабинет соискателя"}})


@stub_router.get("/cabinet/employer")
def cabinet_employer(request: Request, user: Employer) -> Response:
    return render(request, "auth/cabinet_employer.html", {"me": user, "seo": {"title": "Кабинет работодателя"}})


# ------------------------------------------------------------------ подключение

def _asset_url(name: str) -> str:
    try:
        version = str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:
        version = "0"
    return f"/static/{name}?v={version}"


def _fallback_templates(base_url: str) -> Jinja2Templates:
    """Своё окружение шаблонов с теми же функциями, что у основного приложения (нужны base.html и _macros.html)."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.globals.update(
        asset=_asset_url,
        pay_parts=fmt.pay_parts,
        category_icon=lambda c: fmt.CATEGORY_ICONS.get(c, "dots-three-circle"),
        category_label=lambda c: fmt.CATEGORY_LABELS.get(c, str(c).capitalize()),
        work_format_label=lambda c: fmt.WORK_FORMAT_LABELS.get(c, c),
        count_label=fmt.count_label,
        plural=fmt.plural,
        money=fmt.money,
        district_label=fmt.district_label,
        phone_pretty=fmt.phone_pretty,
        phone_href=fmt.phone_href,
        telegram_href=fmt.telegram_href,
        site_url=base_url,
        search_hints=lambda: {"professions": [], "cities": [], "districts": []},
    )
    env.filters["ucfirst"] = lambda s: (s[:1].upper() + s[1:]) if s else s
    env.filters["quote"] = lambda s: quote(str(s), safe="")
    return templates


def install(
    app: FastAPI,
    *,
    db_path: Path | str | None = None,
    mailer: Mailer | None = None,
    site_url: str | None = None,
    secret: str | bytes | None = None,
    clock: Callable[[], datetime] | None = None,
    rate_limits: dict[str, tuple[int, int]] | None = None,
    limiter: RateLimiter | None = None,
    templates: Jinja2Templates | None = None,
    cabinet_stubs: bool = True,
    trust_proxy: bool | None = None,
) -> Accounts:
    """Подключить аккаунты к приложению: маршруты, middleware сессии и CSRF, шаблоны.

    db_path: база (по умолчанию DB_PATH из настроек); mailer: по умолчанию по MAIL_BACKEND;
    templates: окружение шаблонов приложения (иначе берётся app.state.templates, иначе своё с теми же функциями);
    cabinet_stubs=False отключает заглушки /cabinet/seeker и /cabinet/employer, когда есть настоящие кабинеты.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    service.ensure_schema(path)
    base = (site_url or config.get("SITE_URL") or DEFAULT_SITE_URL).rstrip("/")
    acc = Accounts(
        db_path=path,
        mailer=mailer or make_mailer(outbox_dir=path.parent / "outbox"),
        limiter=limiter or RateLimiter(rate_limits),
        secret=csrf_mod.load_secret(secret, path.parent),
        site_url=base,
        clock=clock or (lambda: datetime.now(UTC)),
        trust_proxy=config.get("WEB_TRUST_PROXY") == "1" if trust_proxy is None else trust_proxy,
    )
    app.state.accounts = acc

    if templates is None:
        templates = getattr(app.state, "templates", None)  # create_app может выложить своё окружение шаблонов
    if templates is None:
        templates = _fallback_templates(base)
    templates.env.globals.setdefault("asset", _asset_url)
    app.state.auth_templates = templates

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        target = "/login"
        if exc.next_url and exc.next_url != "/login":
            target = exc.next_url if exc.next_url.startswith("/login?") else "/login?next=" + quote(exc.next_url, safe="/")
        return RedirectResponse(target, status_code=303)

    @app.middleware("http")
    async def accounts_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        state = request.state
        state.user = None
        state.session_touched = False
        # CSRF: зерно в cookie, подпись в форме
        seed = request.cookies.get(CSRF_COOKIE)
        state.csrf_new = False
        if not csrf_mod.valid_seed(seed):
            seed = csrf_mod.new_seed()
            state.csrf_new = True
        state.csrf_seed = seed
        state.csrf_token = csrf_mod.sign(seed or "", acc.secret)
        # сессия
        raw = request.cookies.get(SESSION_COOKIE)
        refresh: datetime | None = None
        stale = False
        if raw:
            def lookup() -> tuple[dict[str, Any] | None, datetime | None]:
                conn = acc.db()
                try:
                    return service.resolve_session(conn, raw, acc.now())
                finally:
                    conn.close()

            state.user, refresh = await run_in_threadpool(lookup)
            stale = state.user is None
        response = await call_next(request)
        if state.csrf_new:
            response.set_cookie(
                CSRF_COOKIE, state.csrf_seed, max_age=30 * 86400, path="/", httponly=True, samesite="lax",
                secure=is_https(request),
            )
        if raw and not state.session_touched:
            if stale:
                clear_session_cookie(response, request)
            elif refresh is not None:
                set_session_cookie(response, request, raw, refresh)
        path_now = request.url.path
        if path_now.startswith(NO_STORE_PREFIXES) or state.user is not None:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    if cabinet_stubs:
        app.include_router(stub_router)
    return acc
