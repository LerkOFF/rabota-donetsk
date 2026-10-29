"""Отклики и переписка: /vacancy/<id>/apply, /responses, /messages, /notifications.

Подключение: rabota.web.messages_routes.install(app, templates=env) после аккаунтов. Все страницы закрыты от индексации,
все POST проверяют CSRF-токен и Origin, чужие отклики и треды отвечают 404. Логика в rabota.messaging.service.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.accounts.deps import (
    Accounts,
    accounts_of,
    check_csrf,
    client_ip,
    get_current_user,
    require_role,
    require_verified,
)
from rabota.accounts.forms import read_form
from rabota.accounts.mailer import Mail, send_safely
from rabota.messaging import notify, rules
from rabota.messaging import service as msg
from rabota.messaging.rules import MessagingError, NotFound
from rabota.web import format as fmt
from rabota.web import queries
from rabota.web.auth_routes import render

router = APIRouter()
Participant = Annotated[dict[str, Any], Depends(require_role("job_seeker", "employer"))]
Verified = Annotated[dict[str, Any], Depends(require_verified)]

# Лимиты частоты (событий, секунд). Добавляются к лимитам аккаунтов, явные настройки приложения главнее.
LIMITS: dict[str, tuple[int, int]] = {
    "apply_user": (20, 3600),
    "apply_ip": (60, 3600),
    "message_user": (20, 300),
    "message_day": (300, 86400),
    "status_user": (200, 3600),
    "block_user": (30, 3600),
    "report_user": (10, 3600),
    "notify_mail": (12, 3600),
    "unsubscribe_ip": (40, 3600),
}

HTTP_BY_CODE = {
    "closed": 409, "duplicate": 409, "forbidden_role": 403, "rate": 429,
}
DONETSK = timezone(timedelta(hours=3))
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
STATUS_TONE = {"new": "new", "viewed": "viewed", "invited": "invited", "rejected": "rejected", "withdrawn": "withdrawn"}


# ------------------------------------------------------------------ вспомогательное

def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


def _id(value: str | None) -> int:
    """Номер из адреса или формы. Мусор и слишком большие числа - как несуществующий объект."""
    if not value or not value.isascii() or not value.isdigit() or len(value) > 12:
        raise HTTPException(404)
    return int(value)


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.isascii() and value.isdigit() and len(value) <= 12 else None


async def _form(request: Request) -> dict[str, str]:
    form = await read_form(request)
    check_csrf(request, form)
    return form


async def _db(acc: Accounts, work: Callable[[Any], Any]) -> Any:
    """Выполнить work(conn) в потоке со своим соединением."""
    def run() -> Any:
        conn = acc.db()
        try:
            return work(conn)
        finally:
            conn.close()

    return await run_in_threadpool(run)


def _redirect(url: str, mail: Mail | None = None, acc: Accounts | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if mail is not None and acc is not None:
        response.background = BackgroundTask(send_safely, acc.mailer, mail)
    return response


def _status_for(exc: MessagingError) -> int:
    return HTTP_BY_CODE.get(exc.code, 422)


def _allowed(acc: Accounts, bucket: str, key: str) -> bool:
    return acc.limiter.allow(bucket, key)


def _local(value: str | None) -> datetime | None:
    dt = fmt.parse_dt(value)
    return dt.astimezone(DONETSK) if dt else None


def _day_label(dt: datetime, today: datetime) -> str:
    delta = (today.date() - dt.date()).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    year = f" {dt.year}" if dt.year != today.year else ""
    return f"{dt.day} {MONTHS[dt.month - 1]}{year}"


def _stamp(value: str | None, now: datetime) -> str:
    """Короткая подпись времени: сегодня "12:30", раньше "29 октября"."""
    dt = _local(value)
    if dt is None:
        return ""
    today = now.astimezone(DONETSK)
    if dt.date() == today.date():
        return dt.strftime("%H:%M")
    return _day_label(dt, today)


def _full_stamp(value: str | None, now: datetime) -> str:
    dt = _local(value)
    if dt is None:
        return ""
    return f"{_day_label(dt, now.astimezone(DONETSK))}, {dt.strftime('%H:%M')}"


def _preview(text: str | None, limit: int = 110) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _decorate_response(row: dict[str, Any], now: datetime, viewer: str) -> dict[str, Any]:
    row["status_label"] = (rules.SEEKER_STATUS_LABELS if viewer == "job_seeker" else rules.STATUS_LABELS)[row["status"]]
    row["when"] = _stamp(row["created_at"], now)
    row["when_full"] = _full_stamp(row["created_at"], now)
    row["excerpt"] = _preview(row.get("cover_letter"))
    row["phone_pretty"] = fmt.phone_pretty(row["contact_phone"]) if row.get("contact_phone") else ""
    row["phone_href"] = fmt.phone_href(row["contact_phone"]) if row.get("contact_phone") else ""
    row["initial"] = (row.get("seeker_name") or row.get("employer_name") or "?")[:1].upper()
    return row


def _paging(page: dict[str, Any]) -> dict[str, Any]:
    return {k: page[k] for k in ("page", "pages", "total", "has_prev", "has_next")}


def _page_no(request: Request) -> int:
    return queries.parse_page(request.query_params.get("page"))


def _view(
    request: Request, name: str, ctx: dict[str, Any], title: str, description: str, status: int = 200
) -> Response:
    ctx = {**ctx, "seo": {"title": title, "description": description, "noindex": True}}
    return render(request, f"messages/{name}", ctx, status)


def _error(exc: MessagingError) -> HTTPException:
    return HTTPException(404) if isinstance(exc, NotFound) else HTTPException(_status_for(exc), exc.text)


# ------------------------------------------------------------------ отклик на вакансию

def _vacancy_card(conn: Any, vacancy_id: int, now: datetime) -> dict[str, Any] | None:
    from rabota.web.app import present_vacancy

    raw = queries.get_vacancy(conn, vacancy_id)
    if raw is None or raw.get("origin") != "direct" or not raw.get("owner_user_id") or not queries.is_listable(raw):
        return None
    return present_vacancy(raw, now)


def _apply_page(
    request: Request, user: dict[str, Any], vacancy: dict[str, Any], *, form: dict[str, Any] | None = None,
    error: str = "", field_errors: dict[str, str] | None = None, status: int = 200, already: dict[str, Any] | None = None,
) -> Response:
    return _view(request, "apply.html", {
        "v": vacancy, "form": form or {"share_phone": bool(user.get("phone"))}, "error": error,
        "errors": field_errors or {}, "already": already, "max_cover": rules.MAX_COVER,
        "phone_pretty": fmt.phone_pretty(user["phone"]) if user.get("phone") else "",
    }, f"Отклик на вакансию: {vacancy['profession_title']}",
        f"Отправьте отклик работодателю на вакансию «{vacancy['profession_title']}».", status)


@router.get("/vacancy/{vacancy_id}/apply")
async def apply_form(request: Request, vacancy_id: str, user: Verified) -> Response:
    acc, vid = _acc(request), _id(vacancy_id)
    now = acc.now()

    def work(conn: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        card = _vacancy_card(conn, vid, now)
        state = msg.apply_state(conn, vid, user["id"]) if card and user["role"] == "job_seeker" else None
        return card, state

    card, state = await _db(acc, work)
    if card is None:
        raise HTTPException(404)
    if user["role"] != "job_seeker":
        return _view(request, "apply_denied.html", {"v": card}, "Откликнуться могут соискатели",
                     "Откликаться на вакансии могут соискатели. Работодатели получают отклики в своём кабинете.", 403)
    return _apply_page(request, user, card, already=state)


@router.post("/vacancy/{vacancy_id}/apply")
async def apply_submit(request: Request, vacancy_id: str, user: Verified) -> Response:
    acc, vid = _acc(request), _id(vacancy_id)
    form = await _form(request)
    if user["role"] != "job_seeker":
        raise HTTPException(403, "Откликаться могут только соискатели.")
    if not (_allowed(acc, "apply_user", str(user["id"])) and _allowed(acc, "apply_ip", client_ip(request))):
        raise HTTPException(429)
    now = acc.now()
    cover, share = form.get("cover", ""), form.get("share_phone") == "1"

    def work(conn: Any) -> tuple[dict[str, Any], MessagingError | None, Any, dict[str, Any] | None]:
        card = _vacancy_card(conn, vid, now)
        if card is None:
            raise NotFound("Вакансия не найдена")
        if form.get("website"):  # ловушка для ботов: делаем вид, что приняли
            return card, None, None, None
        try:
            res = msg.apply(conn, vacancy_id=vid, user=user, cover=cover, share_phone=share, now=now)
        except NotFound:
            raise
        except MessagingError as exc:
            state = msg.apply_state(conn, vid, user["id"]) if exc.code == "duplicate" else None
            return card, exc, None, state
        mail = notify.response_mail(conn, res["application_id"], site_url=acc.site_url, secret=acc.secret)
        if mail is not None and not _allowed(acc, "notify_mail", mail.to):
            mail = None
        return card, None, (res, mail), None

    try:
        card, exc, done, state = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if exc is not None:
        field = {"cover": exc.text} if exc.code in ("too_long", "too_many_links") else {}
        return _apply_page(
            request, user, card, form={"cover": cover, "share_phone": share},
            error="Отклик не отправлен: исправьте сообщение ниже." if field else exc.text, field_errors=field, status=_status_for(exc), already=state,
        )
    if done is None:
        return _redirect("/responses")
    res, mail = done
    return _redirect(f"/responses/{res['application_id']}?sent=1", mail, acc)


# ------------------------------------------------------------------ отклики

@router.get("/responses")
async def responses_list(request: Request, user: Participant) -> Response:
    acc = _acc(request)
    now = acc.now()
    status = request.query_params.get("status", "")
    status = status if status in msg.ALL_STATUSES else ""
    vacancy_id = _int_or_none(request.query_params.get("vacancy"))
    page_no = _page_no(request)

    def work(conn: Any) -> dict[str, Any]:
        page = msg.list_responses(
            conn, user, status=status or None, vacancy_id=vacancy_id if user["role"] == "employer" else None, page=page_no
        )
        mine = []
        if user["role"] == "employer":
            mine = [dict(r) for r in conn.execute(
                "SELECT v.id, v.profession FROM vacancies v WHERE v.owner_user_id = ? AND EXISTS"
                " (SELECT 1 FROM applications a WHERE a.vacancy_id = v.id) ORDER BY v.profession", (user["id"],))]
        return {"page": page, "counts": msg.response_counts(conn, user), "vacancies": mine}

    data = await _db(acc, work)
    items = [_decorate_response(r, now, user["role"]) for r in data["page"]["items"]]
    is_employer = user["role"] == "employer"
    query = "&".join(p for p in (f"status={status}" if status else "", f"vacancy={vacancy_id}" if vacancy_id else "") if p)
    return _view(request, "responses.html", {
        "items": items, "page": _paging(data["page"]), "counts": data["counts"], "status": status, "vacancy_id": vacancy_id,
        "vacancies": data["vacancies"], "is_employer": is_employer, "query": query,
        "flash": "withdrawn" if request.query_params.get("withdrawn") == "1" else "",
        "filters": [("", "Все")] + [(s, rules.STATUS_LABELS[s]) for s in msg.ALL_STATUSES],
    }, "Отклики на вакансии" if is_employer else "Мои отклики",
        "Отклики на ваши вакансии и переписка с кандидатами." if is_employer else "Ваши отклики на вакансии и ответы работодателей.")


@router.get("/responses/{application_id}")
async def response_card(request: Request, application_id: str, user: Participant) -> Response:
    acc, aid = _acc(request), _id(application_id)
    now = acc.now()

    def work(conn: Any) -> dict[str, Any] | None:
        row = msg.get_response(conn, aid, user)
        if row is None:
            return None
        if user["role"] == "employer" and msg.mark_viewed(conn, aid, user["id"], now):
            row = msg.get_response(conn, aid, user)
        return row

    row = await _db(acc, work)
    if row is None:
        raise HTTPException(404)
    return _response_page(request, user, _decorate_response(row, now, user["role"]),
                          sent=request.query_params.get("sent") == "1")


def _response_page(
    request: Request, user: dict[str, Any], row: dict[str, Any], *, sent: bool = False, error: str = "", status: int = 200
) -> Response:
    is_employer = user["role"] == "employer"
    who = row["seeker_name"] if is_employer else row["employer_name"]
    return _view(request, "response.html", {
        "r": row, "is_employer": is_employer, "sent": sent, "error": error,
        "set_statuses": [(s, rules.STATUS_LABELS[s]) for s in rules.EMPLOYER_SET_STATUSES],
    }, f"Отклик: {row['vacancy_title']}, {who}", f"Отклик на вакансию «{row['vacancy_title']}».", status)


@router.post("/responses/{application_id}/status")
async def response_status(request: Request, application_id: str, user: Participant) -> Response:
    acc, aid = _acc(request), _id(application_id)
    form = await _form(request)
    if user["role"] != "employer":
        raise HTTPException(404)
    if not _allowed(acc, "status_user", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()

    def work(conn: Any) -> MessagingError | None:
        try:
            msg.set_status(conn, aid, user["id"], form.get("status", ""), now)
        except NotFound:
            raise
        except MessagingError as exc:
            return exc
        return None

    try:
        exc = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if exc is None:
        return _redirect(f"/responses/{aid}")
    row = await _db(acc, lambda conn: msg.get_response(conn, aid, user))
    if row is None:
        raise HTTPException(404)
    return _response_page(request, user, _decorate_response(row, now, "employer"), error=exc.text, status=_status_for(exc))


@router.post("/responses/{application_id}/withdraw")
async def response_withdraw(request: Request, application_id: str, user: Participant) -> Response:
    acc, aid = _acc(request), _id(application_id)
    await _form(request)
    if user["role"] != "job_seeker":
        raise HTTPException(404)
    now = acc.now()
    try:
        await _db(acc, lambda conn: msg.withdraw(conn, aid, user["id"], now))
    except NotFound:
        raise HTTPException(404) from None
    return _redirect("/responses?withdrawn=1")


@router.get("/cabinet/responses")
def legacy_responses() -> Response:
    return _redirect("/responses")


@router.get("/cabinet/messages")
def legacy_messages() -> Response:
    return _redirect("/messages")


# ------------------------------------------------------------------ переписка

@router.get("/messages")
async def inbox(request: Request, user: Participant) -> Response:
    acc = _acc(request)
    now = acc.now()
    unread_only = request.query_params.get("unread") == "1"
    page_no = _page_no(request)
    data = await _db(acc, lambda conn: {
        "page": msg.list_conversations(conn, user["id"], unread_only=unread_only, page=page_no),
        "unread": msg.unread_total(conn, user["id"]),
    })
    items = data["page"]["items"]
    for t in items:
        t["when"] = _stamp(t["last_message_at"] or t["created_at"], now)
        t["preview"] = _preview(t["last_body"], 90) if t["last_body"] else "Сообщений пока нет"
        t["mine_last"] = t["last_sender"] == user["id"]
        t["initial"] = (t["counterpart_name"] or "?")[:1].upper()
    return _view(request, "inbox.html", {
        "items": items, "page": _paging(data["page"]), "unread_only": unread_only, "unread_total": data["unread"],
        "query": "unread=1" if unread_only else "",
    }, "Сообщения", "Переписка с работодателями и соискателями по вашим откликам.")


def _decorate_messages(items: list[dict[str, Any]], user_id: int, now: datetime, counterpart_read: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    last_day = ""
    first_new_seen = False
    today = now.astimezone(DONETSK)
    for m in items:
        dt = _local(m["created_at"])
        day = _day_label(dt, today) if dt else ""
        m["mine"] = m["sender_user_id"] == user_id
        m["time"] = dt.strftime("%H:%M") if dt else ""
        m["day_break"] = day if day != last_day else ""
        last_day = day
        m["is_new"] = (not m["mine"]) and m["read_at"] is None
        m["new_divider"] = m["is_new"] and not first_new_seen
        first_new_seen = first_new_seen or m["is_new"]
        out.append(m)
    return out


async def _render_thread(
    request: Request, user: dict[str, Any], tid: int, *, before: int | None = None, draft: str = "", error: str = "",
    status: int = 200, flash: str = "", report_error: str = "", report_message: int | None = None,
) -> Response:
    acc = _acc(request)
    now = acc.now()

    def work(conn: Any) -> dict[str, Any] | None:
        conv = msg.get_conversation(conn, tid, user["id"])
        if conv is None:
            return None
        page = msg.list_messages(conn, tid, before=before)
        if before is None:
            msg.mark_read(conn, tid, user["id"], now)
        return {"conv": conv, "page": page}

    data = await _db(acc, work)
    if data is None:
        raise HTTPException(404)
    conv, page = data["conv"], data["page"]
    items = _decorate_messages(page["items"], user["id"], now)
    return _view(request, "thread.html", {
        "c": conv, "messages": items, "has_older": page["has_older"], "older_url": f"/messages/{tid}?before={page['oldest_id']}",
        "is_latest": before is None, "draft": draft, "error": error, "flash": flash, "report_error": report_error,
        "report_message": report_message, "max_message": rules.MAX_MESSAGE, "report_reasons": rules.REPORT_REASONS,
        "max_report": rules.MAX_REPORT_COMMENT, "status_label": rules.STATUS_LABELS.get(conv["app_status"] or "", ""),
        "full_stamp": lambda value: _full_stamp(value, now),
    }, f"Переписка: {conv['title']}", f"Переписка по вакансии «{conv['title']}».", status)


@router.get("/messages/{thread}")
async def thread_view(request: Request, thread: str, user: Participant) -> Response:
    tid = _id(thread)
    before = _int_or_none(request.query_params.get("before"))
    flash = "reported" if request.query_params.get("reported") == "1" else ""
    return await _render_thread(request, user, tid, before=before, flash=flash)


@router.post("/messages/{thread}")
async def thread_send(request: Request, thread: str, user: Participant) -> Response:
    acc, tid = _acc(request), _id(thread)
    form = await _form(request)
    body = form.get("body", "")
    if not (_allowed(acc, "message_user", str(user["id"])) and _allowed(acc, "message_day", str(user["id"]))):
        return await _render_thread(
            request, user, tid, draft=body, status=429,
            error="Вы отправляете сообщения слишком часто. Подождите несколько минут и повторите.",
        )
    now = acc.now()

    def work(conn: Any) -> tuple[MessagingError | None, Mail | None]:
        try:
            mid = msg.send_message(conn, tid, user["id"], body, now)
        except NotFound:
            raise
        except MessagingError as exc:
            return exc, None
        mail = notify.message_mail(conn, tid, mid, site_url=acc.site_url, secret=acc.secret)
        if mail is not None and not _allowed(acc, "notify_mail", mail.to):
            mail = None
        return None, mail

    try:
        exc, mail = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if exc is not None:
        return await _render_thread(request, user, tid, draft=body, error=exc.text, status=_status_for(exc))
    return _redirect(f"/messages/{tid}", mail, acc)


@router.post("/messages/{thread}/block")
async def thread_block(request: Request, thread: str, user: Participant) -> Response:
    acc, tid = _acc(request), _id(thread)
    await _form(request)
    if not _allowed(acc, "block_user", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()
    try:
        await _db(acc, lambda conn: msg.block(conn, tid, user["id"], now))
    except NotFound:
        raise HTTPException(404) from None
    return _redirect(f"/messages/{tid}")


@router.post("/messages/{thread}/unblock")
async def thread_unblock(request: Request, thread: str, user: Participant) -> Response:
    acc, tid = _acc(request), _id(thread)
    await _form(request)
    try:
        await _db(acc, lambda conn: msg.unblock(conn, tid, user["id"]))
    except NotFound:
        raise HTTPException(404) from None
    return _redirect(f"/messages/{tid}")


@router.post("/messages/{thread}/report")
async def thread_report(request: Request, thread: str, user: Participant) -> Response:
    acc, tid = _acc(request), _id(thread)
    form = await _form(request)
    mid = _id(form.get("message_id"))
    if not _allowed(acc, "report_user", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()

    def work(conn: Any) -> MessagingError | None:
        try:
            msg.report_message(conn, tid, mid, user["id"], form.get("reason", ""), form.get("comment", ""), now)
        except NotFound:
            raise
        except MessagingError as exc:
            return exc
        return None

    try:
        exc = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if exc is not None:
        return await _render_thread(
            request, user, tid, report_error=exc.text, report_message=mid, status=_status_for(exc)
        )
    return _redirect(f"/messages/{tid}?reported=1")


# ------------------------------------------------------------------ настройки писем и отписка

def _notifications_page(request: Request, user: dict[str, Any], prefs: dict[str, bool], saved: bool = False) -> Response:
    return _view(request, "notifications.html", {
        "prefs": prefs, "saved": saved, "is_employer": user["role"] == "employer",
    }, "Уведомления по почте", "Какие письма о откликах и сообщениях присылать на почту.")


@router.get("/notifications")
async def notifications_form(request: Request, user: Participant) -> Response:
    acc = _acc(request)
    prefs = await _db(acc, lambda conn: msg.get_prefs(conn, user["id"]))
    return _notifications_page(request, user, prefs, saved=request.query_params.get("saved") == "1")


@router.post("/notifications")
async def notifications_save(request: Request, user: Participant) -> Response:
    acc = _acc(request)
    form = await _form(request)
    await _db(acc, lambda conn: msg.set_prefs(
        conn, user["id"], responses=form.get("responses") == "1", messages=form.get("messages") == "1"))
    return _redirect("/notifications?saved=1")


def _unsub(request: Request, token: str) -> tuple[int, str]:
    acc = _acc(request)
    if not acc.limiter.allow("unsubscribe_ip", client_ip(request)):
        raise HTTPException(429)
    parsed = notify.parse_unsubscribe_token(token, acc.secret)
    if parsed is None:
        raise HTTPException(400, "Ссылка отписки не подошла. Откройте настройки уведомлений в аккаунте.")
    return parsed


KIND_TEXT = {
    "responses": "о новых откликах", "messages": "о новых сообщениях", "all": "об откликах и сообщениях",
}


@router.get("/notifications/unsubscribe/{token}")
async def unsubscribe_form(request: Request, token: str) -> Response:
    _, kind = _unsub(request, token)
    return render(request, "messages/unsubscribe.html", {
        "kind_text": KIND_TEXT[kind], "action": f"/notifications/unsubscribe/{token}", "done": False,
        "seo": {"title": "Отключить письма", "description": "Отключить письма о откликах и сообщениях.", "noindex": True},
    })


@router.post("/notifications/unsubscribe/{token}")
async def unsubscribe_submit(request: Request, token: str) -> Response:
    acc = _acc(request)
    await _form(request)
    user_id, kind = _unsub(request, token)
    await _db(acc, lambda conn: msg.unsubscribe(conn, user_id, kind))
    return render(request, "messages/unsubscribe.html", {
        "kind_text": KIND_TEXT[kind], "action": "", "done": True, "logged_in": get_current_user(request) is not None,
        "seo": {"title": "Письма отключены", "description": "Письма отключены.", "noindex": True},
    })


# ------------------------------------------------------------------ кабинет и страница вакансии

def cabinet_summary(db_path: Any, user: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Счётчики и последние отклики для блока кабинета (вызывается из шаблонов)."""
    empty = {"ok": False}
    if not user or user.get("role") not in ("job_seeker", "employer"):
        return empty
    from rabota.accounts import service

    conn = service.open_db(db_path)
    try:
        counts = msg.response_counts(conn, user)
        page = msg.list_responses(conn, user, per_page=3)
        return {
            "ok": True, "is_employer": user["role"] == "employer", "counts": counts,
            "new_responses": msg.new_responses_count(conn, user["id"]) if user["role"] == "employer" else 0,
            "unread": msg.unread_total(conn, user["id"]),
            "recent": [_decorate_response(r, now, user["role"]) for r in page["items"]],
        }
    finally:
        conn.close()


def apply_box(db_path: Any, vacancy_id: int, user: dict[str, Any] | None) -> dict[str, Any]:
    """Состояние блока отклика на странице вакансии: guest | seeker | applied | employer."""
    if not user:
        return {"mode": "guest"}
    if user.get("role") != "job_seeker":
        return {"mode": "other"}
    from rabota.accounts import service

    conn = service.open_db(db_path)
    try:
        state = msg.apply_state(conn, vacancy_id, user["id"])
        row = conn.execute("SELECT phone FROM users WHERE id = ?", (user["id"],)).fetchone()
    finally:
        conn.close()
    phone = row["phone"] if row else None
    return {"mode": "applied" if state else "seeker", "state": state, "phone": fmt.phone_pretty(phone) if phone else ""}


def install(app: FastAPI, *, templates: Any | None = None) -> None:
    """Маршруты, лимиты и функции для шаблонов. Вызывается после install аккаунтов."""
    acc = _acc_of_app(app)
    for bucket, limit in LIMITS.items():
        acc.limiter.limits.setdefault(bucket, limit)
    env = getattr(templates, "env", None)
    if env is not None:
        env.globals["cabinet_messaging"] = lambda user: cabinet_summary(acc.db_path, user, acc.now())
        env.globals["apply_box"] = lambda vacancy_id, user: apply_box(acc.db_path, vacancy_id, user)
    app.include_router(router)


def _acc_of_app(app: FastAPI) -> Accounts:
    acc = getattr(app.state, "accounts", None)
    if acc is None:
        raise RuntimeError("Сначала подключите аккаунты (auth_routes.install)")
    return acc  # type: ignore[no-any-return]


__all__ = ["apply_box", "cabinet_summary", "install", "router"]
