"""Заявка «Это моя компания», архив вакансий компании и список заявок работодателя.

    /company/{id}/claim            форма заявки, ввод кода, состояние заявки (гость и не работодатель получают пояснение)
    /company/{id}/claim/code       POST: код из письма
    /company/{id}/claim/resend     POST: новый код
    /company/{id}/claim/note       POST: дополнить заявку (ответ на просьбу администратора)
    /company/{id}/claim/cancel     POST: отозвать заявку
    /company/{id}/archive          вкладка «Архив» на странице работодателя (noindex)
    /cabinet/claims                мои заявки

Подключение: claim_routes.install(app) после аккаунтов и до размещения вакансий. Все страницы заявки закрыты от индекса и
не кэшируются, POST проверяют CSRF и лимиты. Правила и состояния: rabota.employers_auto.claims.
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
from rabota.employers_auto import archive as arch
from rabota.employers_auto import cards, claims
from rabota.web import format as fmt
from rabota.web.auth_routes import render

router = APIRouter()

LIMITS: dict[str, tuple[int, int]] = {
    "claim_ip": (10, 3600),        # новых заявок с одного адреса в час
    "claim_user": (60, 3600),      # действий по заявкам с одного аккаунта в час
    "claim_code_ip": (30, 3600),   # попыток ввести код с одного адреса в час
}

CODE_TEXT = {
    "format": "Введите шесть цифр из письма.",
    "bad": "Код не подошёл. Проверьте цифры в письме или запросите новый код.",
    "expired": "Срок кода вышел. Запросите новый код.",
    "locked": "Слишком много неверных попыток. Запросите новый код.",
    "none": "Код для этой заявки больше не нужен или не запрашивался.",
}
FLASH = {
    "sent": "Заявка отправлена. Мы выслали код на указанную почту.",
    "review": "Заявка отправлена на проверку. Администратор свяжется с вами по указанному телефону.",
    "code_ok": "Почта подтверждена. Заявка ушла на проверку администратору.",
    "resent": "Новый код отправлен.",
    "noted": "Дополнение к заявке сохранено.",
    "cancelled": "Заявка отозвана.",
}
DESCRIPTION = "Заявка на карточку компании на сайте Работа Донецк."


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


def _go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _employer(request: Request) -> dict[str, Any] | None:
    user = get_current_user(request)
    return user if user and user.get("verified") and user.get("role") == "employer" else None


def _company_or_404(conn: Any, company_id: int) -> dict[str, Any]:
    company = cards.get(conn, company_id)
    if company is None or company.get("blocked_at"):
        raise HTTPException(404)
    return company


def _after_merge(company: dict[str, Any], suffix: str) -> RedirectResponse | None:
    if company.get("merged_into"):
        return RedirectResponse(f"/company/{company['merged_into']}{suffix}", status_code=301)
    return None


def _view(request: Request, ctx: dict[str, Any], title: str, status: int = 200) -> Response:
    ctx = {**ctx, "seo": {"title": title, "description": DESCRIPTION, "noindex": True}}
    resp = render(request, "claim/page.html", ctx, status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _page_context(conn: Any, company: dict[str, Any], user: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    prior = claims.latest_for(conn, int(company["id"]), int(user["id"])) if user else None
    ctx: dict[str, Any] = {
        "company": company, "me_employer": user if user and user.get("role") == "employer" else None, "claim": prior,
        "statuses": claims.STATUSES, "methods": claims.METHODS, "form": {}, "errors": {}, "error": "", "flash": "",
        "owned_by_other": bool(company.get("owner_user_id") and (not user or company["owner_user_id"] != user["id"])),
        "is_owner": bool(user and company.get("owner_user_id") == user["id"]),
        "domains": sorted(claims.company_domains(conn, company))[:3],
        "phones_known": bool(claims.company_phones(conn, company)),
        "login_href": "/login?next=" + quote(f"/company/{company['id']}/claim", safe="/"),
        "limit_message": claims.MESSAGE_LIMIT,
    }
    ctx.update(extra)
    return ctx


@router.get("/company/{company_id}/claim")
async def claim_page(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = get_current_user(request)
    flash = FLASH.get(request.query_params.get("ok", ""), "")

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        moved = _after_merge(company, "/claim")
        if moved:
            return moved
        ctx = _page_context(conn, company, user, flash=flash)
        return _view(request, ctx, f"Заявка на карточку: {company['name']}")

    return await _db(acc, work)


def _redirect_back(company_id: int, ok: str = "") -> RedirectResponse:
    return _go(f"/company/{company_id}/claim" + (f"?ok={ok}" if ok else ""))


async def _form_for(request: Request, user: dict[str, Any], bucket: str = "claim_user") -> dict[str, str]:
    form = await read_form(request)
    check_csrf(request, form)
    if not _acc(request).limiter.allow(bucket, str(user["id"])):
        raise HTTPException(429)
    return form


@router.post("/company/{company_id}/claim")
async def claim_submit(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        raise HTTPException(403, "Заявку подаёт вошедший работодатель.")
    form = await _form_for(request, user)
    if not acc.limiter.allow("claim_ip", client_ip(request)):
        raise HTTPException(429)
    now = acc.now()
    ip = client_ip(request)
    method = form.get("method", "")
    contact = form.get("email", "") if method == "email" else form.get("phone", "")

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        moved = _after_merge(company, "/claim")
        if moved:
            return moved
        errors: dict[str, str] = {}
        if form.get("consent") != "1":
            errors["consent"] = "Нужно согласие на обработку контактов для проверки заявки."
        outcome = None
        if not errors:
            try:
                outcome = claims.submit(conn, acc.secret, company, user, method=method, contact=contact,
                                        message=form.get("message", ""), ip=ip, site_url=acc.site_url, now=now)
            except claims.ClaimError as exc:
                field = {"email": "email", "free_mail": "email", "domain": "email", "phone": "phone",
                         "phone_unknown": "phone", "method": "method"}.get(exc.code, "")
                if field:
                    errors[field] = exc.text
                else:
                    ctx = _page_context(conn, company, user, form=form, error=exc.text)
                    return _view(request, ctx, f"Заявка на карточку: {company['name']}", 409)
        if errors:
            ctx = _page_context(conn, company, user, form=form, errors=errors)
            return _view(request, ctx, f"Заявка на карточку: {company['name']}", 422)
        resp = _redirect_back(company_id, "sent" if outcome.status == "sent" else "review")
        if outcome.mail is not None:
            resp.background = BackgroundTask(send_safely, acc.mailer, outcome.mail)
        return resp

    return await _db(acc, work)


@router.post("/company/{company_id}/claim/code")
async def claim_code(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        raise HTTPException(403)
    form = await _form_for(request, user)
    if not acc.limiter.allow("claim_code_ip", client_ip(request)):
        raise HTTPException(429)
    now = acc.now()

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        claim = claims.latest_for(conn, company_id, int(user["id"]))
        if claim is None:
            raise HTTPException(404)
        result = claims.check_code(conn, acc.secret, claim["id"], int(user["id"]), form.get("code", ""), now)
        if result == "ok":
            return _redirect_back(company_id, "code_ok")
        ctx = _page_context(conn, company, user, errors={"code": CODE_TEXT.get(result, CODE_TEXT["bad"])})
        return _view(request, ctx, f"Заявка на карточку: {company['name']}", 422)

    return await _db(acc, work)


@router.post("/company/{company_id}/claim/resend")
async def claim_resend(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        raise HTTPException(403)
    await _form_for(request, user)
    now = acc.now()

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        claim = claims.latest_for(conn, company_id, int(user["id"]))
        if claim is None:
            raise HTTPException(404)
        try:
            outcome = claims.resend_code(conn, acc.secret, claim["id"], int(user["id"]), acc.site_url, now)
        except claims.ClaimError as exc:
            ctx = _page_context(conn, company, user, error=exc.text)
            return _view(request, ctx, f"Заявка на карточку: {company['name']}", 429 if exc.code == "cooldown" else 409)
        resp = _redirect_back(company_id, "resent")
        resp.background = BackgroundTask(send_safely, acc.mailer, outcome.mail)
        return resp

    return await _db(acc, work)


@router.post("/company/{company_id}/claim/note")
async def claim_note(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        raise HTTPException(403)
    form = await _form_for(request, user)
    now = acc.now()

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        claim = claims.latest_for(conn, company_id, int(user["id"]))
        if claim is None:
            raise HTTPException(404)
        try:
            claims.add_note(conn, claim["id"], int(user["id"]), form.get("note", ""), now)
        except claims.ClaimError as exc:
            ctx = _page_context(conn, company, user, errors={"note": exc.text})
            return _view(request, ctx, f"Заявка на карточку: {company['name']}", 422)
        return _redirect_back(company_id, "noted")

    return await _db(acc, work)


@router.post("/company/{company_id}/claim/cancel")
async def claim_cancel(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        raise HTTPException(403)
    await _form_for(request, user)
    now = acc.now()

    def work(conn: Any) -> Response:
        _company_or_404(conn, company_id)
        claim = claims.latest_for(conn, company_id, int(user["id"]))
        if claim is None or not claims.withdraw(conn, claim["id"], int(user["id"]), now):
            raise HTTPException(404)
        return _redirect_back(company_id, "cancelled")

    return await _db(acc, work)


@router.get("/cabinet/claims")
async def my_claims(request: Request) -> Response:
    acc = _acc(request)
    user = _employer(request)
    if user is None:
        user = get_current_user(request)
        if user is None:
            return _go("/login?next=/cabinet/claims")
        raise HTTPException(403, "Эта страница недоступна для вашей роли.")
    now = acc.now()

    def work(conn: Any) -> list[dict[str, Any]]:
        rows = claims.for_user(conn, int(user["id"]))
        for r in rows:
            r["created_label"] = fmt.date_label(r["created_at"], now)
            r["status_label"] = claims.STATUSES.get(r["status"], r["status"])
        return rows

    rows = await _db(acc, work)
    resp = render(request, "claim/my.html", {
        "rows": rows, "seo": {"title": "Мои заявки на карточки компаний", "description": DESCRIPTION, "noindex": True}})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ архив

def period_label(published: str | None, last_seen: str | None, now: Any) -> str:
    start, end = fmt.date_label(published, now), fmt.date_label(last_seen or published, now)
    if start and end and start != end:
        return f"с {start} по {end}"
    return start or end or ""


@router.get("/company/{company_id}/archive")
async def company_archive(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    try:
        page_no = min(max(int(request.query_params.get("page", "1")), 1), 100_000)
    except ValueError:
        page_no = 1
    now = acc.now()

    def work(conn: Any) -> Response:
        company = _company_or_404(conn, company_id)
        moved = _after_merge(company, "/archive")
        if moved:
            return moved
        rows, total = arch.company_archive(conn, company_id, page_no)
        counts = cards.counts(conn, company_id)
        for r in rows:
            r["title"] = (r["profession"] or "").strip().capitalize()
            r["pay"] = fmt.pay_parts(r["pay_min"], r["pay_max"], r["pay_period"])
            r["location"] = fmt.location_label(r["city"], r["district"])
            r["period"] = period_label(r["published_at"], r["last_seen_at"], now)
        pages = max(1, -(-total // arch.ARCHIVE_PER_PAGE))
        page = min(page_no, pages)
        base = f"/company/{company_id}/archive"
        ctx = {
            "company": company, "rows": rows, "total": total, "counts": counts, "page": page, "pages": pages,
            "prev_url": (base + (f"?page={page - 1}" if page > 2 else "")) if page > 1 else "",
            "next_url": f"{base}?page={page + 1}" if page < pages else "",
            "seo": {
                "title": f"Архив вакансий: {company['name']}",
                "description": f"Прошлые вакансии работодателя {company['name']}: должности, периоды и оплата. Они закрыты и недоступны для отклика.",
                "noindex": True, "robots": "noindex, follow",
                "breadcrumbs": [("Главная", "/"), ("Вакансии", "/vacancies"), (company["name"], f"/company/{company_id}"),
                                ("Архив", base)],
            },
        }
        return render(request, "claim/archive.html", ctx)

    return await _db(acc, work)


def install(app: FastAPI) -> None:
    acc = getattr(app.state, "accounts", None)
    if acc is not None:
        for bucket, limit in LIMITS.items():
            acc.limiter.limits.setdefault(bucket, limit)
    app.include_router(router)
