"""Подписки на вакансии: /alerts, /alerts/new, подтверждение почты, привязка Telegram, отписка по ссылке.

Подключение: rabota.web.alerts_routes.install(app, templates=env) после аккаунтов. Все страницы закрыты от индексации,
все POST проверяют CSRF-токен и Origin (кроме отписки одним нажатием из почтового клиента: там подпись в адресе).
Логика в rabota.alerts.service.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.accounts.deps import Accounts, accounts_of, check_csrf, client_ip, get_current_user
from rabota.accounts.forms import read_form
from rabota.accounts.mailer import send_safely
from rabota.alerts import conf, filters, mailtext, service
from rabota.web.auth_routes import render

router = APIRouter()

LIMITS: dict[str, tuple[int, int]] = {
    "alert_new_ip": (12, 3600),
    "alert_new_email": (3, 3600),
    "alert_tg_ip": (10, 3600),
    "alert_code_ip": (30, 3600),
    "alert_unsub_ip": (60, 3600),
    "alert_user": (60, 3600),
}
FLASH = {
    "added": "Подписка включена. Новые вакансии придут на вашу почту.",
    "exists": "Такая подписка у вас уже есть, мы её включили.",
    "paused": "Подписка на паузе: письма не приходят.",
    "resumed": "Подписка снова включена. Придут только вакансии, которые появятся с этого момента.",
    "deleted": "Подписка удалена.",
    "freq": "Частота изменена.",
}


# ------------------------------------------------------------------ вспомогательное

def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


async def _db(acc: Accounts, work: Callable[[Any], Any]) -> Any:
    def run() -> Any:
        conn = acc.db()
        try:
            return work(conn)
        finally:
            conn.close()

    return await run_in_threadpool(run)


async def _form(request: Request) -> dict[str, str]:
    form = await read_form(request)
    check_csrf(request, form)
    return form


def _redirect(url: str, background: BackgroundTask | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if background is not None:
        response.background = background
    return response


def _view(request: Request, name: str, ctx: dict[str, Any], title: str, description: str, status: int = 200) -> Response:
    ctx = {**ctx, "seo": {"title": title, "description": description, "noindex": True}}
    return render(request, f"alerts/{name}", ctx, status)


def _id(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit() or len(value) > 12:
        raise HTTPException(404)
    return int(value)


def _tg_cfg() -> conf.TelegramConfig:
    return conf.TelegramConfig.from_env()


def _tg_open(conn: Any) -> bool:
    cfg = _tg_cfg()
    return cfg.bot_ready and bool(conf.setting(conn, "tg_bot_enabled"))


def _row_view(row: Any, cfg: conf.TelegramConfig, secret: bytes = b"") -> dict[str, Any]:
    data = service.filters_of(row)
    return {
        "id": row["id"], "title": row["title"], "channel": row["channel"], "frequency": row["frequency"],
        "frequency_label": service.FREQUENCY_LABELS[row["frequency"]], "status": row["status"],
        "status_label": service.STATUS_LABELS[row["status"]], "last_sent_at": row["last_sent_at"],
        "sent_count": row["sent_count"], "last_error": row["last_error"], "url": filters.vacancies_url("", data),
        "waiting_chat": row["channel"] == "telegram" and not row["tg_chat_id"],
        "bot_url": cfg.bot_url, "ref": service.make_token(row["id"], "ref", secret) if secret else "",
    }


def _filters_from(form: Any) -> dict[str, Any]:
    return filters.normalize({k: form.get(k) for k in filters.FILTER_KEYS if form.get(k) not in (None, "")})


def _new_context(request: Request, conn: Any, data: dict[str, Any], form: dict[str, str] | None = None,
                 errors: dict[str, str] | None = None) -> dict[str, Any]:
    form = form or {}
    return {
        "filters": data, "title": filters.describe(data) if data else "", "fields": alert_fields(data),
        "results_url": filters.vacancies_url("", data), "tg_enabled": _tg_open(conn),
        "mail_enabled": bool(conf.setting(conn, "alerts_email_enabled")),
        "channel": form.get("channel") if form.get("channel") in service.CHANNELS else "email",
        "frequency": form.get("frequency") if form.get("frequency") in service.FREQUENCIES else "daily",
        "email": form.get("email", ""), "consent": form.get("consent") == "1", "errors": errors or {},
        "frequency_labels": service.FREQUENCY_LABELS,
    }


def alert_fields(data: Any) -> list[tuple[str, str]]:
    """Скрытые поля формы подписки по фильтрам выдачи (объект Filters или словарь)."""
    if not isinstance(data, dict):
        data = filters.to_dict(data)
    return [(k, "1" if v is True else str(v)) for k, v in filters.normalize(data).items()]


# ------------------------------------------------------------------ новая подписка

@router.get("/alerts/new")
async def new_form(request: Request) -> Response:
    acc = _acc(request)
    data = filters.normalize(request.query_params)
    ctx = await _db(acc, lambda conn: _new_context(request, conn, data))
    return _view(request, "new.html", ctx, "Подписка на новые вакансии",
                 "Сохраните поиск, и новые вакансии придут на почту или в Telegram сразу или раз в день.")


@router.post("/alerts/new")
async def new_submit(request: Request) -> Response:
    acc = _acc(request)
    form = await _form(request)
    data = _filters_from(form)
    user = get_current_user(request)
    now = acc.now()
    channel, frequency = form.get("channel", "email"), form.get("frequency", "daily")
    errors: dict[str, str] = {}

    def page(status: int = 422) -> Response:
        ctx = _db_ctx(form, errors)
        return _view(request, "new.html", ctx, "Подписка на новые вакансии",
                     "Сохраните поиск, и новые вакансии придут на почту или в Telegram сразу или раз в день.", status)

    def _db_ctx(f: dict[str, str], e: dict[str, str]) -> dict[str, Any]:
        conn = acc.db()
        try:
            return _new_context(request, conn, data, f, e)
        finally:
            conn.close()

    ip = client_ip(request)
    if not acc.limiter.allow("alert_new_ip", ip):
        raise HTTPException(429)

    def work(conn: Any) -> Any:
        if channel == "telegram":
            if not _tg_open(conn):
                raise service.AlertError("channel", "Подписка через Telegram сейчас недоступна. Выберите почту.")
            if user is None and not acc.limiter.allow("alert_tg_ip", ip):
                raise HTTPException(429)
            row, code = service.subscribe_telegram(conn, user, data, frequency=frequency, now=now)
            return "telegram", row, code
        service.check_options(channel, frequency)
        if not conf.setting(conn, "alerts_email_enabled"):
            raise service.AlertError("channel", "Подписки по почте временно отключены. Попробуйте позже или выберите Telegram.")
        if user is not None:
            row, created = service.subscribe_user(conn, user, data, channel="email", frequency=frequency, now=now)
            return "user", row, created
        address = (form.get("email") or "").strip()
        if form.get("consent") != "1":
            raise service.AlertError("consent", "Нужно согласие на получение писем с вакансиями.")
        if not acc.limiter.allow("alert_new_email", address.lower()):
            raise HTTPException(429)
        row, code = service.subscribe_guest(conn, address, data, frequency=frequency, secret=acc.secret, now=now)
        mail = mailtext.confirm_mail(row, code, site_url=acc.site_url, secret=acc.secret) if code else None
        return "guest", row, mail

    try:
        kind, row, extra = await _db(acc, work)
    except service.AlertError as exc:
        errors[exc.code if exc.code in ("email", "consent", "channel", "frequency") else "form"] = exc.text
        return page()
    if kind == "telegram":
        ref = service.make_token(row["id"], "ref", acc.secret)
        return _redirect(f"/alerts/telegram/{ref}" + (f"?c={extra}" if extra else ""))
    if kind == "user":
        return _redirect("/alerts?m=" + ("added" if extra else "exists"))
    ref = service.make_token(row["id"], "ref", acc.secret)
    task = BackgroundTask(send_safely, acc.mailer, extra) if extra is not None else None
    return _redirect(f"/alerts/code/{ref}", task)


# ------------------------------------------------------------------ подтверждение почты гостя

def _ref(request: Request, ref: str) -> int:
    acc = _acc(request)
    if not acc.limiter.allow("alert_code_ip", client_ip(request)):
        raise HTTPException(429)
    sub_id = service.parse_token(ref, "ref", acc.secret)
    if sub_id is None:
        raise HTTPException(404)
    return sub_id


CODE_ERRORS = {
    "bad": "Код не подошёл. Проверьте цифры в письме.",
    "none": "Код не подошёл или устарел. Запросите новый.",
    "expired": "Код устарел. Запросите новый.",
    "locked": "Слишком много неверных попыток. Запросите новый код.",
}


@router.get("/alerts/code/{ref}")
async def code_form(request: Request, ref: str) -> Response:
    sub_id = _ref(request, ref)
    acc = _acc(request)
    row = await _db(acc, lambda conn: service.get(conn, sub_id))
    if row is None or row["channel"] != "email":
        raise HTTPException(404)
    return _code_page(request, ref, row["email"], request.query_params.get("sent") == "1")


def _code_page(request: Request, ref: str, email: str, sent: bool = False, error: str = "", status: int = 200) -> Response:
    return _view(request, "code.html", {"ref": ref, "email": email, "sent": sent, "error": error},
                 "Подтвердите почту", "Введите код из письма, чтобы включить подписку на новые вакансии.", status)


@router.post("/alerts/code/{ref}")
async def code_submit(request: Request, ref: str) -> Response:
    acc = _acc(request)
    form = await _form(request)
    sub_id = _ref(request, ref)
    result, row = await _db(acc, lambda conn: (
        service.check_code(conn, sub_id, form.get("code", ""), acc.secret, acc.now()), service.get(conn, sub_id)))
    if row is None:
        raise HTTPException(404)
    if result == "ok":
        return _view(request, "done.html", {"title": row["title"], "frequency": service.FREQUENCY_LABELS[row["frequency"]]},
                     "Подписка включена", "Подписка на новые вакансии подтверждена, письма будут приходить на вашу почту.")
    return _code_page(request, ref, row["email"], error=CODE_ERRORS[result], status=422)


@router.post("/alerts/code/{ref}/resend")
async def code_resend(request: Request, ref: str) -> Response:
    acc = _acc(request)
    await _form(request)
    sub_id = _ref(request, ref)

    def work(conn: Any) -> Any:
        row = service.get(conn, sub_id)
        if row is None or row["channel"] != "email" or row["status"] != "pending":
            return None
        code = service.issue_code(conn, sub_id, acc.secret, acc.now())
        return mailtext.confirm_mail(row, code, site_url=acc.site_url, secret=acc.secret) if code else None

    mail = await _db(acc, work)
    task = BackgroundTask(send_safely, acc.mailer, mail) if mail is not None else None
    return _redirect(f"/alerts/code/{ref}?sent=1", task)


@router.get("/alerts/confirm/{token}")
async def confirm_form(request: Request, token: str) -> Response:
    acc = _acc(request)
    sub_id = service.parse_token(token, "confirm", acc.secret)
    row = await _db(acc, lambda conn: service.get(conn, sub_id)) if sub_id else None
    if row is None:
        return _view(request, "confirm.html", {"row": None, "action": ""}, "Ссылка не подошла",
                     "Ссылка подтверждения подписки не подошла или устарела. Оформите подписку заново.", 400)
    return _view(request, "confirm.html", {"row": row, "action": f"/alerts/confirm/{token}", "done": False},
                 "Подтвердите подписку", "Подтвердите подписку на новые вакансии одним нажатием.")


@router.post("/alerts/confirm/{token}")
async def confirm_submit(request: Request, token: str) -> Response:
    acc = _acc(request)
    await _form(request)
    sub_id = service.parse_token(token, "confirm", acc.secret)
    if sub_id is None:
        raise HTTPException(404)
    ok, row = await _db(acc, lambda conn: (service.confirm_by_link(conn, sub_id, acc.now()), service.get(conn, sub_id)))
    if not ok or row is None:
        return _view(request, "confirm.html", {"row": None, "action": ""}, "Ссылка не подошла",
                     "Ссылка подтверждения подписки устарела. Оформите подписку заново.", 400)
    return _view(request, "done.html", {"title": row["title"], "frequency": service.FREQUENCY_LABELS[row["frequency"]]},
                 "Подписка включена", "Подписка на новые вакансии подтверждена, письма будут приходить на вашу почту.")


# ------------------------------------------------------------------ Telegram

@router.get("/alerts/telegram/{ref}")
async def telegram_page(request: Request, ref: str) -> Response:
    sub_id = _ref(request, ref)
    acc = _acc(request)
    row = await _db(acc, lambda conn: service.get(conn, sub_id))
    if row is None or row["channel"] != "telegram":
        raise HTTPException(404)
    cfg = _tg_cfg()
    code = request.query_params.get("c", "")
    link = cfg.start_link(code) if code and cfg.bot_ready and row["status"] == "pending" else ""
    return _view(request, "telegram.html", {
        "ref": ref, "row": _row_view(row, cfg), "link": link, "connected": row["status"] == "active" and bool(row["tg_chat_id"]),
        "bot_name": cfg.bot_username, "bot_ready": cfg.bot_ready,
    }, "Подключите Telegram", "Откройте бота в Telegram и нажмите Start: подписка на новые вакансии подключится сама.")


@router.post("/alerts/telegram/{ref}")
async def telegram_renew(request: Request, ref: str) -> Response:
    acc = _acc(request)
    await _form(request)
    sub_id = _ref(request, ref)

    def work(conn: Any) -> str | None:
        row = service.get(conn, sub_id)
        if row is None or row["channel"] != "telegram" or row["status"] != "pending":
            return None
        return service.issue_tg_code(conn, sub_id, acc.now())

    code = await _db(acc, work)
    return _redirect(f"/alerts/telegram/{ref}" + (f"?c={code}" if code else ""))


# ------------------------------------------------------------------ отписка по ссылке из письма

def _unsub_id(request: Request, token: str) -> int:
    acc = _acc(request)
    if not acc.limiter.allow("alert_unsub_ip", client_ip(request)):
        raise HTTPException(429)
    sub_id = service.parse_token(token, "unsub", acc.secret)
    if sub_id is None:
        raise HTTPException(400, "Ссылка отписки не подошла. Откройте список подписок в аккаунте.")
    return sub_id


@router.get("/alerts/unsubscribe/{token}")
async def unsubscribe_form(request: Request, token: str) -> Response:
    sub_id = _unsub_id(request, token)
    row = await _db(_acc(request), lambda conn: service.get(conn, sub_id))
    return _view(request, "unsubscribe.html", {
        "row": row, "action": f"/alerts/unsubscribe/{token}", "done": row is None or row["status"] == "unsubscribed",
        "logged_in": get_current_user(request) is not None,
    }, "Отписаться от вакансий", "Отключить письма с новыми вакансиями по этой подписке.")


@router.post("/alerts/unsubscribe/{token}")
async def unsubscribe_submit(request: Request, token: str) -> Response:
    acc = _acc(request)
    form = await read_form(request)
    one_click = form.get("List-Unsubscribe") == "One-Click"  # почтовый клиент нажал «Отписаться»: подпись в адресе
    if not one_click:
        check_csrf(request, form)
    sub_id = _unsub_id(request, token)
    row = await _db(acc, lambda conn: service.unsubscribe(conn, sub_id, acc.now()))
    if one_click:
        return Response("ok", media_type="text/plain")
    return _view(request, "unsubscribe.html", {
        "row": row, "action": "", "done": True, "logged_in": get_current_user(request) is not None,
    }, "Вы отписались", "Письма с новыми вакансиями по этой подписке отключены.")


# ------------------------------------------------------------------ кабинет

@router.get("/alerts")
async def alerts_list(request: Request) -> Response:
    acc = _acc(request)
    user = get_current_user(request)
    cfg = _tg_cfg()
    if user is None:
        return _view(request, "list.html", {"guest": True, "items": [], "flash": "", "limit": 0, "tg_enabled": False},
                     "Подписки на вакансии", "Подписки на новые вакансии: список, пауза и удаление. Войдите в аккаунт, чтобы управлять ими.")

    def work(conn: Any) -> Any:
        rows = service.list_for_user(conn, user)
        return [_row_view(r, cfg, acc.secret) for r in rows], int(conf.setting(conn, "alerts_max_per_user")), _tg_open(conn)

    items, limit, tg_enabled = await _db(acc, work)
    key = request.query_params.get("m", "")
    return _view(request, "list.html", {
        "guest": False, "items": items, "flash": FLASH.get(key, ""), "limit": limit, "tg_enabled": tg_enabled,
        "active_count": sum(1 for i in items if i["status"] != "unsubscribed"),
    }, "Подписки на вакансии", "Ваши подписки на новые вакансии: частота, пауза, подключение Telegram и удаление.")


async def _owned_action(request: Request, sub_id: str, work: Callable[[Any, dict[str, Any], int, dict[str, str]], Any]) -> Any:
    acc = _acc(request)
    user = get_current_user(request)
    if user is None:
        return _redirect("/login?next=/alerts")
    form = await _form(request)
    if not acc.limiter.allow("alert_user", str(user["id"])):
        raise HTTPException(429)
    number = _id(sub_id)
    return await _db(acc, lambda conn: work(conn, user, number, form))


@router.post("/alerts/{sub_id}/pause")
async def pause(request: Request, sub_id: str) -> Response:
    result = await _owned_action(request, sub_id, lambda c, u, n, f: service.pause(c, u, n))
    if isinstance(result, Response):
        return result
    if not result:
        raise HTTPException(404)
    return _redirect("/alerts?m=paused")


@router.post("/alerts/{sub_id}/resume")
async def resume(request: Request, sub_id: str) -> Response:
    acc = _acc(request)
    result = await _owned_action(request, sub_id, lambda c, u, n, f: service.resume(c, u, n, acc.now()))
    if isinstance(result, Response):
        return result
    if not result:
        raise HTTPException(404)
    return _redirect("/alerts?m=resumed")


@router.post("/alerts/{sub_id}/delete")
async def delete(request: Request, sub_id: str) -> Response:
    result = await _owned_action(request, sub_id, lambda c, u, n, f: service.delete(c, u, n))
    if isinstance(result, Response):
        return result
    if not result:
        raise HTTPException(404)
    return _redirect("/alerts?m=deleted")


@router.post("/alerts/{sub_id}/frequency")
async def frequency(request: Request, sub_id: str) -> Response:
    def work(conn: Any, user: dict[str, Any], number: int, form: dict[str, str]) -> Any:
        try:
            return service.set_frequency(conn, user, number, form.get("frequency", ""))
        except service.AlertError:
            return "bad"

    result = await _owned_action(request, sub_id, work)
    if isinstance(result, Response):
        return result
    if result == "bad":
        raise HTTPException(422, "Выберите, как часто присылать: сразу или раз в день.")
    if not result:
        raise HTTPException(404)
    return _redirect("/alerts?m=freq")


@router.post("/alerts/{sub_id}/telegram")
async def add_telegram(request: Request, sub_id: str) -> Response:
    acc = _acc(request)

    def work(conn: Any, user: dict[str, Any], number: int, form: dict[str, str]) -> Any:
        if not _tg_open(conn):
            return "off"
        try:
            return service.add_telegram_to(conn, user, number, acc.now())
        except service.AlertError as exc:
            return exc

    result = await _owned_action(request, sub_id, work)
    if isinstance(result, Response):
        return result
    if result == "off":
        raise HTTPException(409, "Подписка через Telegram сейчас недоступна.")
    if isinstance(result, service.AlertError):
        raise HTTPException(422, result.text)
    if result is None:
        raise HTTPException(404)
    row, code = result
    ref = service.make_token(row["id"], "ref", acc.secret)
    return _redirect(f"/alerts/telegram/{ref}" + (f"?c={code}" if code else ""))


# ------------------------------------------------------------------ подключение

def install(app: FastAPI, *, templates: Any | None = None) -> None:
    """Маршруты, лимиты и функции для шаблонов. Вызывается после install аккаунтов."""
    acc = getattr(app.state, "accounts", None)
    if acc is None:
        raise RuntimeError("Сначала подключите аккаунты (auth_routes.install)")
    for bucket, limit in LIMITS.items():
        acc.limiter.limits.setdefault(bucket, limit)
    env = getattr(templates, "env", None)
    if env is not None:
        cfg = conf.TelegramConfig.from_env()
        env.globals["alert_fields"] = alert_fields
        env.globals["alerts_quote"] = lambda s: quote(str(s), safe="")
        env.globals["tg_channel_url"] = cfg.channel_url
        env.globals["tg_bot_url"] = cfg.bot_url if cfg.bot_ready else ""
    app.include_router(router)


__all__ = ["alert_fields", "install", "router"]
