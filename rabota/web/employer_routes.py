"""Размещение вакансии и работодатель: /razmestit, код из письма, «Готово», логотип компании.

Подключение: rabota.web.employer_routes.install(app, ...) из create_app, после аккаунтов.

Гость: одна страница, затем код из письма на почту (черновик в `post_drafts`, аккаунт создаётся после кода).
Вошедший работодатель: вакансия сразу привязывается к его компании. В обоих случаях работает автомодерация
(rabota.employers.automod), а после публикации открывается страница «Готово» с советами и кнопкой «Поднять».
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota import cabinet
from rabota.accounts import codes, service
from rabota.accounts.deps import (
    SESSION_COOKIE,
    Accounts,
    accounts_of,
    check_csrf,
    require_role,
    rotate_csrf,
    set_session_cookie,
)
from rabota.accounts.mailer import send_safely
from rabota.cabinet import CabinetError
from rabota.employers import automod, logo, posting, quickform
from rabota.web import queries as q

MAX_FORM = 32 * 1024
MAX_UPLOAD = logo.MAX_BYTES + 64 * 1024
EXTRA_LIMITS = {"post_code_email": (5, 3600), "post_code_fail_ip": (30, 3600), "logo_user": (10, 3600), "raise_user": (30, 3600)}

router = APIRouter()
Employer = Annotated[dict[str, Any], Depends(require_role("employer"))]

BAD_CODE_TEXT = "Код не подошёл. Проверьте цифры в письме или отправьте новый код."
LOCK_CODE_TEXT = "Слишком много неверных попыток. Отправьте новый код и введите его."
EXPIRED_CODE_TEXT = "Срок кода вышел. Отправьте новый код."
COOLDOWN_TEXT = "Новый код можно запросить через минуту."
LIMIT_TEXT = "Новых кодов для этой вакансии больше нельзя: заполните форму ещё раз."
FORMAT_TEXT = "Введите шесть цифр из письма."

READY_DONE = {
    "raised": "Вакансия поднята: срок размещения начался заново.",
    "reopened": "Вакансия снова открыта.",
}
READY_ERR = {
    "too_soon": "Поднимать вакансию можно раз в сутки.",
    "not_allowed": "Поднять можно только опубликованную вакансию.",
    "rate": "Слишком много действий подряд. Подождите немного и повторите.",
}


class Hooks:
    """Что приложение передаёт маршрутам: рендер, часы, лимит, база."""

    def __init__(self, **kw: Any):
        self.__dict__.update(kw)

    render: Callable[..., Response]
    clock: Callable[[], datetime]
    limiter: Any
    client_ip: Callable[[Request], str]
    same_origin: Callable[[Request], bool]
    premoderate: bool
    db_path: Path | str | None


def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


async def read_form(request: Request, limit: int = MAX_FORM) -> dict[str, str]:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "Слишком большой запрос")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=60)
    return {k: v[0] for k, v in parsed.items()}


def poster(request: Request) -> dict[str, Any] | None:
    """Вошедший работодатель: его вакансии привязываются к аккаунту и компании. Гость и другие роли: None."""
    user = getattr(request.state, "user", None)
    if isinstance(user, Mapping) and user.get("role") == "employer":
        return dict(user)
    return None


async def _run(acc: Accounts, work: Callable[[Any], Any]) -> Any:
    def go() -> Any:
        conn = acc.db()
        try:
            return work(conn)
        finally:
            conn.close()

    return await run_in_threadpool(go)


def install(
    app: FastAPI, *, render: Callable[..., Response], clock: Callable[[], datetime], limiter: Any,
    client_ip: Callable[[Request], str], same_origin: Callable[[Request], bool], premoderate: bool,
    db_path: Path | str | None,
) -> None:
    acc = getattr(app.state, "accounts", None)
    if acc is None:
        raise RuntimeError("Сначала подключите аккаунты (auth_routes.install)")
    for bucket, limit in EXTRA_LIMITS.items():
        acc.limiter.limits.setdefault(bucket, limit)
    app.state.employer_hooks = Hooks(
        render=render, clock=clock, limiter=limiter, client_ip=client_ip, same_origin=same_origin,
        premoderate=premoderate, db_path=db_path)
    app.include_router(router)


def _h(request: Request) -> Hooks:
    return request.app.state.employer_hooks  # type: ignore[no-any-return]


# ------------------------------------------------------------------ форма размещения

def _post_page(request: Request, form: dict[str, str] | None = None, errors: dict[str, str] | None = None,
               status: int = 200) -> Response:
    from rabota.web.postform import form_options

    user = poster(request)
    ctx: dict[str, Any] = {
        "form": form or {"city": "Донецк", "pay_period": "месяц"}, "errors": errors or {}, **form_options(),
        "form_action": "/razmestit", "is_employer": user is not None,
        "seo": {
            "title": "Разместить вакансию в Донецке бесплатно",
            "description": "Опубликуйте вакансию в Донецке и ДНР за минуту: профессия, район, оплата, график, телефон. "
                           "Бесплатно, после короткой проверки, аккаунт работодателя создаётся сам.",
            "breadcrumbs": [("Главная", "/"), ("Разместить вакансию", "/razmestit")],
            **({"noindex": True} if errors else {}),
        },
    }
    return _h(request).render(request, "post.html", ctx, status)


def _employer_defaults(acc: Accounts, user: dict[str, Any]) -> dict[str, str]:
    conn = acc.db()
    try:
        company = cabinet.get_company(conn, int(user["id"])) or {}
    finally:
        conn.close()
    return {
        "employer": company.get("name") or "",
        "phone": company.get("phone") or user.get("phone") or "",
        "email": company.get("contact_email") or "",
    }


@router.get("/razmestit")
async def post_form(request: Request) -> Response:
    form = {"city": "Донецк", "pay_period": "месяц"}
    user = poster(request)
    if user:
        defaults = await run_in_threadpool(_employer_defaults, _acc(request), user)
        form.update({k: v for k, v in defaults.items() if v})
    return _post_page(request, form)


@router.post("/razmestit")
async def post_submit(request: Request) -> Response:
    h = _h(request)
    acc = _acc(request)
    if not h.same_origin(request):
        raise HTTPException(403, "Запрос с другого сайта")
    form = await read_form(request)
    user = poster(request)
    if not user and form.get("website"):  # ловушка для ботов: делаем вид, что приняли
        return RedirectResponse("/razmestit/spasibo", status_code=303)
    check_csrf(request, form)
    if not h.limiter.allow("post", h.client_ip(request)):
        raise HTTPException(429)
    now = acc.now()

    if user:
        vacancy, cleaned, errors = quickform.validate_employer(form)
        if errors or vacancy is None:
            return _post_page(request, cleaned, errors, 422)

        def save(conn: Any) -> int:
            company_id = cabinet.ensure_company(conn, user, vacancy.employer or "", vacancy.city or "")
            company = cabinet.get_company(conn, int(user["id"])) or {}
            vacancy.employer = vacancy.employer or company.get("name") or None
            vid = q.add_site_vacancy(
                conn, vacancy, premoderate=h.premoderate, owner_user_id=int(user["id"]), company_id=company_id)
            automod.moderate_new(conn, vid, now, premoderate=h.premoderate)
            return vid

        vacancy_id = await _run(acc, save)
        return RedirectResponse(f"/razmestit/gotovo/{vacancy_id}", status_code=303)

    vacancy, cleaned, errors, email = quickform.validate_guest(form)
    if errors or vacancy is None:
        return _post_page(request, cleaned, errors, 422)
    if not acc.limiter.allow("post_code_email", email):
        return _post_page(request, cleaned, {"account_email": "На эту почту уже отправлено много кодов. Попробуйте через час."}, 429)

    def draft(conn: Any) -> tuple[str, str]:
        return posting.create_draft(
            conn, acc.secret, email=email, form=quickform.draft_form(cleaned), consent_at=now, ip=h.client_ip(request), now=now)

    token, code = await _run(acc, draft)
    mail = posting.post_mail(acc.site_url, email, cleaned.get("employer", ""), code)
    response = RedirectResponse(f"/razmestit/kod/{token}", status_code=303)
    response.background = BackgroundTask(send_safely, acc.mailer, mail)
    return response


@router.get("/razmestit/spasibo")
def post_thanks(request: Request) -> Response:
    return _h(request).render(request, "post_thanks.html", {"seo": {
        "title": "Вакансия отправлена", "noindex": True,
        "description": "Вакансия отправлена на проверку и появится в ленте, как только мы убедимся, "
                       "что это настоящее предложение о работе."}})


# ------------------------------------------------------------------ код из письма

def _code_page(request: Request, draft: dict[str, Any], *, error: str = "", info: str = "", status: int = 200) -> Response:
    token = request.path_params["token"]
    return _h(request).render(request, "post_code.html", {
        "token": token, "email_hint": posting.email_hint(draft["email"]), "error": error, "info": info,
        "vacancy_title": (draft["form"].get("profession") or "").strip(),
        "seo": {"title": "Код из письма: размещение вакансии", "noindex": True,
                "description": "Введите шестизначный код из письма, чтобы опубликовать вакансию."},
    }, status)


def _gone(request: Request) -> Response:
    return _h(request).render(request, "post_code_gone.html", {"seo": {
        "title": "Ссылка устарела", "noindex": True,
        "description": "Страница подтверждения вакансии больше не действует. Заполните форму размещения ещё раз."}}, 410)


@router.get("/razmestit/kod/{token}")
async def code_form(request: Request, token: str) -> Response:
    acc = _acc(request)
    draft = await _run(acc, lambda conn: posting.get_draft(conn, token, acc.now()))
    if draft is None:
        return _gone(request)
    return _code_page(request, draft, error=EXPIRED_CODE_TEXT if draft["expired"] else "")


@router.post("/razmestit/kod/{token}")
async def code_submit(request: Request, token: str) -> Response:
    h = _h(request)
    acc = _acc(request)
    form = await read_form(request, 4096)
    check_csrf(request, form)
    ip = h.client_ip(request)
    if acc.limiter.blocked("post_code_fail_ip", ip):
        raise HTTPException(429)
    now = acc.now()
    code = codes.clean_code(form.get("code", ""))
    old_session = request.cookies.get(SESSION_COOKIE, "")
    ua = request.headers.get("user-agent", "")

    def work(conn: Any) -> tuple[str, dict[str, Any] | None, posting.Published | None]:
        draft = posting.get_draft(conn, token, now)
        if draft is None:
            return "none", None, None
        if code is None:
            return "format", draft, None
        status, ok_draft = posting.check_code(conn, acc.secret, token, code, now)
        if status != "ok" or ok_draft is None:
            return status, draft, None
        result = posting.publish(conn, ok_draft, now, premoderate=h.premoderate, ip=ip, user_agent=ua)
        if result.kind == "employer" and old_session:
            service.delete_session(conn, old_session)
        return "ok", ok_draft, result

    status, draft, result = await _run(acc, work)
    if status == "none" or draft is None:
        return _gone(request)
    if status != "ok" or result is None:
        if status != "format":
            acc.limiter.record("post_code_fail_ip", ip)
        text = {"format": FORMAT_TEXT, "expired": EXPIRED_CODE_TEXT, "locked": LOCK_CODE_TEXT}.get(status, BAD_CODE_TEXT)
        return _code_page(request, draft, error=text, status=429 if status == "locked" else 400)
    if result.kind == "blocked":
        return _h(request).render(request, "post_blocked.html", {"seo": {
            "title": "Аккаунт заблокирован", "noindex": True,
            "description": "Аккаунт с этой почтой заблокирован, вакансия не опубликована."}}, 403)
    if result.kind == "invalid":
        return _gone(request)
    if result.kind == "anonymous" or result.session == "":
        return RedirectResponse("/razmestit/spasibo", status_code=303)
    response = RedirectResponse(
        f"/razmestit/gotovo/{result.vacancy_id}{'?new=1' if result.new_account else ''}", status_code=303)
    assert result.session_expires is not None
    set_session_cookie(response, request, result.session, result.session_expires)
    rotate_csrf(request)
    request.state.session_touched = True
    return response


@router.post("/razmestit/kod/{token}/povtor")
async def code_resend(request: Request, token: str) -> Response:
    h = _h(request)
    acc = _acc(request)
    form = await read_form(request, 4096)
    check_csrf(request, form)
    if not acc.limiter.allow("code_send_ip", h.client_ip(request)):
        raise HTTPException(429)
    now = acc.now()

    def work(conn: Any) -> tuple[str, str | None, dict[str, Any] | None]:
        status, code = posting.resend_code(conn, acc.secret, token, now)
        return status, code, posting.get_draft(conn, token, now)

    status, code, draft = await _run(acc, work)
    if draft is None:
        return _gone(request)
    if status == "ok" and code and acc.limiter.allow("post_code_email", draft["email"]):
        page = _code_page(request, draft, info="Мы отправили новый код. Старый больше не действует.")
        page.background = BackgroundTask(
            send_safely, acc.mailer, posting.post_mail(acc.site_url, draft["email"], draft["form"].get("employer", ""), code))
        return page
    text = {"cooldown": COOLDOWN_TEXT, "limit": LIMIT_TEXT}.get(status, "На эту почту уже отправлено много кодов. Попробуйте через час.")
    return _code_page(request, draft, error=text, status=429 if status != "cooldown" else 200)


# ------------------------------------------------------------------ «Готово»

def ready_tips(item: dict[str, Any]) -> list[dict[str, str]]:
    """Советы после публикации: короткие и по делу, без выдуманных обещаний."""
    tips = [
        {"icon": "phone", "title": "Держите телефон под рукой",
         "text": "Большинство соискателей звонят в первые часы после публикации. Ответьте быстро: это видно в показателях компании."},
        {"icon": "currency-rub", "title": "Укажите оплату",
         "text": "Вакансии с оплатой получают больше откликов. Если вилка широкая, напишите, от чего зависит доход."},
        {"icon": "buildings", "title": "Заполните карточку компании",
         "text": "Логотип, описание и сайт повышают доверие. Страницу компании видно под каждой вашей вакансией."},
        {"icon": "arrow-fat-up", "title": "Поднимайте вакансию",
         "text": "Раз в сутки её можно поднять в начало ленты, а ещё через 21 день срок размещения заканчивается."},
    ]
    return tips


@router.get("/razmestit/gotovo/{vacancy_id}")
async def ready_page(request: Request, user: Employer, vacancy_id: int) -> Response:
    acc = _acc(request)
    now = acc.now()
    try:
        item = await _run(acc, lambda conn: cabinet.get_item(conn, int(user["id"]), vacancy_id, now))
    except CabinetError:
        raise HTTPException(404) from None
    done = request.query_params.get("done", "")
    err = request.query_params.get("err", "")
    flags = automod.flag_labels(item.get("automod_flags"))
    company = await _run(acc, lambda conn: cabinet.get_company(conn, int(user["id"])))
    return _h(request).render(request, "post_ready.html", {
        "item": item, "tips": ready_tips(item), "flash": READY_DONE.get(done, ""), "problem": READY_ERR.get(err, ""),
        "new_account": request.query_params.get("new") == "1", "manual": bool(flags) and item["state"].key == "pending",
        "company": company, "email": user.get("email", ""),
        "seo": {"title": "Вакансия принята", "noindex": True,
                "description": "Вакансия принята: статус проверки, советы по откликам и кнопка «Поднять»."},
    })


@router.post("/razmestit/gotovo/{vacancy_id}/podnyat")
async def ready_raise(request: Request, user: Employer, vacancy_id: int) -> Response:
    acc = _acc(request)
    form = await read_form(request, 4096)
    check_csrf(request, form)
    if not acc.limiter.allow("raise_user", str(user["id"])):
        return RedirectResponse(f"/razmestit/gotovo/{vacancy_id}?err=rate", status_code=303)
    try:
        result = await _run(acc, lambda conn: cabinet.raise_vacancy(conn, int(user["id"]), vacancy_id, acc.now()))
    except CabinetError as exc:
        if exc.code == "not_found":
            raise HTTPException(404) from None
        return RedirectResponse(f"/razmestit/gotovo/{vacancy_id}?err={exc.code}", status_code=303)
    return RedirectResponse(f"/razmestit/gotovo/{vacancy_id}?done={result}", status_code=303)


# ------------------------------------------------------------------ логотип

def _logos_dir(request: Request) -> Path:
    h = _h(request)
    return logo.logo_dir(h.db_path or _acc(request).db_path)


@router.get("/company/{company_id}/logo")
async def logo_file(request: Request, company_id: int) -> Response:
    acc = _acc(request)
    row = await _run(acc, lambda conn: conn.execute(
        "SELECT logo_path, blocked_at FROM companies WHERE id = ?", (company_id,)).fetchone())
    name = logo.safe_name(row["logo_path"], company_id) if row and not row["blocked_at"] else None
    path = _logos_dir(request) / name if name else None
    if path is None or not path.is_file():
        raise HTTPException(404)
    return FileResponse(
        path, media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
                 "Content-Disposition": "inline"})


async def _read_upload(request: Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 2 МБ.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            raise HTTPException(413, "Файл больше 2 МБ.")
    try:
        return logo.parse_multipart(request.headers.get("content-type", ""), body)
    except logo.LogoError as exc:
        raise HTTPException(400, exc.text) from None


def _back(code: str, error: str = "") -> RedirectResponse:
    url = f"/cabinet/company?logo={code}"
    if error:
        url += "&logo_err=" + error
    return RedirectResponse(url + "#logo", status_code=303)


@router.post("/cabinet/company/logo")
async def logo_upload(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    fields, files = await _read_upload(request)
    check_csrf(request, fields)
    if not acc.limiter.allow("logo_user", str(user["id"])):
        raise HTTPException(429)
    upload = files.get("logo")
    if upload is None or not upload[1]:
        return _back("err", "no_file")
    try:
        png = logo.process_logo(upload[1])
    except logo.LogoError as exc:
        return _back("err", exc.code)
    directory = _logos_dir(request)

    def work(conn: Any) -> bool:
        company = cabinet.get_company(conn, int(user["id"]))
        if company is None:
            return False
        name = logo.save_logo(directory, int(company["id"]), png)
        conn.execute("UPDATE companies SET logo_path = ? WHERE id = ?", (name, company["id"]))
        conn.commit()
        return True

    if not await _run(acc, work):
        return _back("err", "no_company")
    return _back("ok")


@router.post("/cabinet/company/logo/delete")
async def logo_delete(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    form = await read_form(request, 4096)
    check_csrf(request, form)
    directory = _logos_dir(request)

    def work(conn: Any) -> None:
        company = cabinet.get_company(conn, int(user["id"]))
        if company is None:
            return
        logo.remove_logo(directory, int(company["id"]))
        conn.execute("UPDATE companies SET logo_path = NULL WHERE id = ?", (company["id"],))
        conn.commit()

    await _run(acc, work)
    return _back("removed")
