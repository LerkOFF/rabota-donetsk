"""Резюме: кабинет соискателя (/cabinet/resumes) и поиск резюме для работодателя (/employers/resumes).

Подключение: rabota.web.resume_routes.install(app, templates=env) после аккаунтов и откликов. Все страницы закрыты от
индексации и не кэшируются, гость уходит на вход, чужая роль получает 403, чужое резюме отвечает 404.
Публичных страниц резюме нет. Все POST проверяют CSRF-токен и Origin. Логика в rabota.resumes.service.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota import legal
from rabota.accounts.deps import Accounts, accounts_of, check_csrf, require_role
from rabota.accounts.mailer import send_safely
from rabota.messaging import notify
from rabota.pipeline.dictionaries import DISTRICT_PATTERNS
from rabota.resumes import rules, service
from rabota.resumes.service import NotFound, ResumeError
from rabota.web import queries
from rabota.web.auth_routes import render

router = APIRouter()
Seeker = Annotated[dict[str, Any], Depends(require_role("job_seeker"))]
Employer = Annotated[dict[str, Any], Depends(require_role("employer"))]

MAX_BODY = 24 * 1024

# Лимиты частоты (событий, секунд). Явные настройки приложения главнее.
LIMITS: dict[str, tuple[int, int]] = {
    "resume_save": (30, 3600),
    "resume_create": (6, 86400),
    "resume_action": (60, 3600),
    "resume_search": (300, 3600),
    "resume_card": (200, 3600),
    "resume_contact": (30, 3600),
    "resume_invite": (20, 3600),
}

FLASH = {
    "created": "Резюме сохранено.",
    "saved": "Изменения сохранены.",
    "hidden": "Резюме скрыто: работодатели его больше не видят. Показать снова можно в любой момент.",
    "refreshed": "Резюме обновлено: оно снова считается свежим.",
    "deleted": "Резюме удалено.",
}
PROBLEMS = {
    "limit": f"У вас уже {rules.MAX_RESUMES} резюме: это предел. Удалите ненужное или отредактируйте существующее.",
    "rate": "Слишком много действий подряд. Подождите немного и повторите.",
}
CABINET_DESCRIPTION = "Ваши резюме на сайте Работа Донецк: видимость, заполненность и подсказки, что добавить."
SEARCH_DESCRIPTION = "Поиск резюме соискателей Донецка: профессия, район, зарплата, опыт и график. Контакты открываются по кнопке."


class Form:
    """Поля формы: get - первое значение, getlist - все (флажки с одним именем)."""

    def __init__(self, data: dict[str, list[str]]):
        self.data = data

    def get(self, name: str, default: str = "") -> str:
        values = self.data.get(name)
        return values[0] if values else default

    def getlist(self, name: str) -> list[str]:
        return list(self.data.get(name, []))


# ------------------------------------------------------------------ вспомогательное

def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


def _id(value: str | None) -> int:
    if not value or not value.isascii() or not value.isdigit() or len(value) > 12:
        raise HTTPException(404)
    return int(value)


async def _form(request: Request) -> Form:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Слишком большой запрос")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=60)
    form = Form(parsed)
    check_csrf(request, {k: v[0] for k, v in parsed.items() if v})
    return form


async def _db(acc: Accounts, work: Callable[[Any], Any]) -> Any:
    def run() -> Any:
        conn = acc.db()
        try:
            return work(conn)
        finally:
            conn.close()

    return await run_in_threadpool(run)


def _go(url: str, mail: Any = None, acc: Accounts | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if mail is not None and acc is not None:
        response.background = BackgroundTask(send_safely, acc.mailer, mail)
    return response


def _allowed(acc: Accounts, bucket: str, key: str) -> bool:
    return acc.limiter.allow(bucket, key)


def _view(request: Request, name: str, ctx: dict[str, Any], title: str, description: str, status: int = 200) -> Response:
    ctx = {**ctx, "seo": {"title": title, "description": description, "noindex": True}}
    return render(request, f"resumes/{name}", ctx, status)


def _page_no(request: Request) -> int:
    return queries.parse_page(request.query_params.get("page"))


FORM_LISTS = {
    "professions": rules.PROFESSION_SUGGESTIONS,
    "districts": tuple(DISTRICT_PATTERNS),
    "pay_periods": tuple(rules.PAY_PERIOD_LABELS.items()),
    "formats": tuple(rules.WORK_FORMAT_LABELS.items()),
    "employments": tuple(rules.EMPLOYMENT_LABELS.items()),
    "visibilities": tuple((k, rules.VISIBILITY_LABELS[k], rules.VISIBILITY_HINTS[k]) for k in rules.VISIBILITIES),
}


# ------------------------------------------------------------------ соискатель: список

@router.get("/cabinet/resumes")
async def resumes_list(request: Request, user: Seeker) -> Response:
    acc = _acc(request)
    now = acc.now()
    items = await _db(acc, lambda conn: service.list_own(conn, user["id"], now))
    return _view(request, "list.html", {
        "items": items, "limit": rules.MAX_RESUMES, "can_create": len(items) < rules.MAX_RESUMES,
        "flash": FLASH.get(request.query_params.get("done", ""), ""),
        "problem": PROBLEMS.get(request.query_params.get("err", ""), ""),
    }, "Мои резюме", CABINET_DESCRIPTION)


# ------------------------------------------------------------------ соискатель: форма

def _form_page(
    request: Request, user: dict[str, Any], *, resume: dict[str, Any] | None, form: dict[str, Any],
    errors: dict[str, str], status: int = 200,
) -> Response:
    places = list(form.get("places") or [])
    while len(places) < rules.MAX_PLACES:
        places.append({"place": "", "role": "", "period": ""})
    title = "Новое резюме" if resume is None else "Редактирование резюме"
    consent_html = legal.render_markdown(
        legal.source_text(rules.CONSENT_SLUG), legal.variables(), id_prefix="resume-consent-").html
    return _view(request, "form.html", {
        "resume": resume, "form": {**form, "places": places}, "errors": errors, "lists": FORM_LISTS,
        "limits": {"about": rules.MAX_ABOUT, "skills": rules.MAX_SKILLS, "skill": rules.MAX_SKILL_LEN},
        "consent_html": consent_html, "consent_version": rules.consent_version(),
        "pay_label": "Желаемая зарплата, ₽",
    }, title, "Анкета резюме на сайте Работа Донецк: профессия, зарплата, опыт, навыки и видимость для работодателей.", status)


@router.get("/cabinet/resumes/new")
async def resume_new(request: Request, user: Seeker) -> Response:
    acc = _acc(request)
    count = await _db(acc, lambda conn: service.count_own(conn, user["id"]))
    if count >= rules.MAX_RESUMES:
        return _go("/cabinet/resumes?err=limit")
    form: dict[str, Any] = {
        "city": "Донецк", "visibility": "public", "contact_phone": user.get("phone") or "", "places": [],
        "work_formats": [], "employments": [], "consent": False,
    }
    return _form_page(request, user, resume=None, form=form, errors={})


def _clean_from(form: Form, consented: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    return rules.validate(form, consented_now=consented)


@router.post("/cabinet/resumes/new")
async def resume_create(request: Request, user: Seeker) -> Response:
    acc = _acc(request)
    form = await _form(request)
    if form.get("website"):  # ловушка для ботов: делаем вид, что приняли
        return _go("/cabinet/resumes?done=created")
    if not (_allowed(acc, "resume_create", str(user["id"])) and _allowed(acc, "resume_save", str(user["id"]))):
        raise HTTPException(429)
    clean, errors = _clean_from(form)
    if errors:
        return _form_page(request, user, resume=None, form=_echo(clean), errors=errors, status=422)
    now = acc.now()
    try:
        await _db(acc, lambda conn: service.create(conn, user["id"], clean, now))
    except ResumeError as exc:
        return _go("/cabinet/resumes?err=" + exc.code)
    return _go("/cabinet/resumes?done=created")


def _echo(clean: dict[str, Any]) -> dict[str, Any]:
    """Очищенные значения в виде, пригодном для повторного показа формы."""
    out = dict(clean)
    out["skills"] = ", ".join(clean["skills"])
    out["pay_min"] = clean["pay_min"] or ""
    out["experience_years"] = "" if clean["experience_years"] is None else clean["experience_years"]
    return out


@router.get("/cabinet/resumes/{resume_id}/edit")
async def resume_edit_form(request: Request, resume_id: str, user: Seeker) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    resume = await _db(acc, lambda conn: service.get_own(conn, rid, user["id"]))
    if resume is None:
        raise HTTPException(404)
    form = service.to_form(resume)
    if request.query_params.get("show") == "1" and form["visibility"] == "hidden":
        form["visibility"] = "public"
    return _form_page(request, user, resume=service.present(resume, acc.now()), form=form, errors={})


@router.post("/cabinet/resumes/{resume_id}/edit")
async def resume_edit_save(request: Request, resume_id: str, user: Seeker) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    form = await _form(request)
    if not _allowed(acc, "resume_save", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()
    resume = await _db(acc, lambda conn: service.get_own(conn, rid, user["id"]))
    if resume is None:
        raise HTTPException(404)
    if form.get("website"):
        return _go("/cabinet/resumes?done=saved")
    consented = bool(service.to_form(resume)["consent"])
    clean, errors = _clean_from(form, consented)
    if errors:
        return _form_page(request, user, resume=service.present(resume, now), form=_echo(clean), errors=errors, status=422)
    try:
        await _db(acc, lambda conn: service.update(conn, rid, user["id"], clean, now))
    except NotFound:
        raise HTTPException(404) from None
    return _go("/cabinet/resumes?done=saved")


async def _own_action(request: Request, resume_id: str, user: dict[str, Any], work: Callable[[Any, int], None], done: str) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    await _form(request)
    if not _allowed(acc, "resume_action", str(user["id"])):
        return _go("/cabinet/resumes?err=rate")
    try:
        await _db(acc, lambda conn: work(conn, rid))
    except NotFound:
        raise HTTPException(404) from None
    return _go(f"/cabinet/resumes?done={done}")


@router.post("/cabinet/resumes/{resume_id}/hide")
async def resume_hide(request: Request, resume_id: str, user: Seeker) -> Response:
    now = _acc(request).now()
    return await _own_action(request, resume_id, user, lambda conn, rid: service.hide(conn, rid, user["id"], now), "hidden")


@router.post("/cabinet/resumes/{resume_id}/refresh")
async def resume_refresh(request: Request, resume_id: str, user: Seeker) -> Response:
    now = _acc(request).now()
    return await _own_action(request, resume_id, user, lambda conn, rid: service.refresh(conn, rid, user["id"], now), "refreshed")


@router.get("/cabinet/resumes/{resume_id}/delete")
async def resume_delete_confirm(request: Request, resume_id: str, user: Seeker) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    now = acc.now()

    def work(conn: Any) -> dict[str, Any] | None:
        resume = service.get_own(conn, rid, user["id"])
        if resume is None:
            return None
        item = service.present(resume, now)
        item["applications"] = int(conn.execute("SELECT COUNT(*) FROM applications WHERE resume_id = ?", (rid,)).fetchone()[0])
        return item

    item = await _db(acc, work)
    if item is None:
        raise HTTPException(404)
    return _view(request, "delete.html", {"item": item}, f"Удалить резюме «{item['title']}»",
                 "Подтверждение удаления резюме: что произойдёт с откликами и поиском.")


@router.post("/cabinet/resumes/{resume_id}/delete")
async def resume_delete(request: Request, resume_id: str, user: Seeker) -> Response:
    return await _own_action(request, resume_id, user, lambda conn, rid: service.delete(conn, rid, user["id"]), "deleted")


# ------------------------------------------------------------------ работодатель: поиск и карточка

def _query_of(filters: dict[str, Any]) -> str:
    base = {"q": filters["q"], "district": filters["district"], "pay_to": filters["pay_to"] or "",
            "period": filters["period"] if filters["pay_to"] else "", "exp": filters["exp"],
            "employment": filters["employment"], "format": filters["format"],
            "sort": filters["sort"] if filters["sort"] != "fresh" else ""}
    return urlencode({k: v for k, v in base.items() if v != "" and v is not None})


@router.get("/employers/resumes")
async def resumes_search(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    if not _allowed(acc, "resume_search", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()
    filters = service.parse_filters(request.query_params)
    page_no = _page_no(request)

    def work(conn: Any) -> dict[str, Any]:
        return {"page": service.search(conn, user["id"], filters, now, page_no),
                "total": service.total_searchable(conn, user["id"], now)}

    data = await _db(acc, work)
    page = data["page"]
    active = any(filters[k] for k in ("q", "district", "pay_to", "exp", "employment", "format"))
    return _view(request, "search.html", {
        "items": page["items"], "page": {k: page[k] for k in ("page", "pages", "total", "has_prev", "has_next")},
        "f": filters, "query": _query_of(filters), "active": active, "all_total": data["total"], "lists": FORM_LISTS,
        "experience_filters": tuple(rules.EXPERIENCE_FILTERS.items()),
        "sorts": tuple((k, v[0]) for k, v in service.SORTS.items()),
    }, "Поиск резюме", SEARCH_DESCRIPTION)


def _card_page(
    request: Request, resume: dict[str, Any], *, vacancies: list[dict[str, Any]], opened: bool, error: str = "",
    invite_error: str = "", invite_text: str = "", invite_vacancy: int | None = None, flash: str = "", status: int = 200,
) -> Response:
    title = f"Резюме: {resume['title']}, {resume['display_name']}"
    return _view(request, "card.html", {
        "r": resume, "vacancies": vacancies, "opened": opened, "error": error, "invite_error": invite_error,
        "invite_text": invite_text or service.default_invite_text(resume, vacancies[0] if vacancies else None),
        "invite_vacancy": invite_vacancy or (vacancies[0]["id"] if vacancies else None), "flash": flash,
        "max_invite": rules.MAX_INVITE_TEXT,
    }, title, f"Резюме соискателя: {resume['title']}. Опыт, навыки, желаемая зарплата; контакты открываются по кнопке.", status)


def _load_card(conn: Any, rid: int, user: dict[str, Any], now: Any, *, count_view: bool) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    resume = service.get_for_employer(conn, rid, user["id"], now)
    if resume is None:
        raise NotFound()
    if count_view:
        service.record_view(conn, rid, user["id"], now)
    opened = service.contact_opened(conn, rid, user["id"])
    return resume, service.employer_vacancies(conn, user["id"]), opened


@router.get("/employers/resumes/{resume_id}")
async def resume_card(request: Request, resume_id: str, user: Employer) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    if not _allowed(acc, "resume_card", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()
    try:
        resume, vacancies, opened = await _db(acc, lambda conn: _load_card(conn, rid, user, now, count_view=True))
    except NotFound:
        raise HTTPException(404) from None
    return _card_page(request, resume, vacancies=vacancies, opened=opened,
                      flash="invited" if request.query_params.get("invited") == "1" else "")


@router.post("/employers/resumes/{resume_id}/contact")
async def resume_contact(request: Request, resume_id: str, user: Employer) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    await _form(request)
    if not _allowed(acc, "resume_contact", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()

    def work(conn: Any) -> tuple[dict[str, Any], list[dict[str, Any]], bool, str]:
        resume, vacancies, _ = _load_card(conn, rid, user, now, count_view=False)
        try:
            service.open_contact(conn, rid, user["id"], now)
        except ResumeError as exc:
            return resume, vacancies, False, exc.text
        return resume, vacancies, True, ""

    try:
        resume, vacancies, _opened, problem = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if problem:
        return _card_page(request, resume, vacancies=vacancies, opened=False, error=problem, status=429)
    return _go(f"/employers/resumes/{rid}#contacts")


@router.post("/employers/resumes/{resume_id}/invite")
async def resume_invite(request: Request, resume_id: str, user: Employer) -> Response:
    acc, rid = _acc(request), _id(resume_id)
    form = await _form(request)
    if not _allowed(acc, "resume_invite", str(user["id"])):
        raise HTTPException(429)
    now = acc.now()
    vid_raw = form.get("vacancy_id")
    vacancy_id = int(vid_raw) if vid_raw.isascii() and vid_raw.isdigit() and len(vid_raw) <= 12 else 0
    text = form.get("text")

    def work(conn: Any) -> tuple[Any, ...]:
        resume, vacancies, opened = _load_card(conn, rid, user, now, count_view=False)
        try:
            res = service.invite(conn, resume_id=rid, employer=user, vacancy_id=vacancy_id, text=text, now=now)
        except ResumeError as exc:
            return resume, vacancies, opened, exc, None, None
        mail = notify.message_mail(conn, res["conversation_id"], res["message_id"], site_url=acc.site_url, secret=acc.secret)
        if mail is not None and not _allowed(acc, "notify_mail", mail.to):
            mail = None
        return resume, vacancies, opened, None, res, mail

    try:
        resume, vacancies, opened, exc, res, mail = await _db(acc, work)
    except NotFound:
        raise HTTPException(404) from None
    if exc is not None:
        if exc.code == "exists":
            return _go(f"/messages/{exc.extra['conversation_id']}")
        if exc.code == "not_found":
            raise HTTPException(404)
        status = 429 if exc.code == "invite_limit" else 422
        return _card_page(request, resume, vacancies=vacancies, opened=opened, invite_error=exc.text, invite_text=text,
                          invite_vacancy=vacancy_id or None, status=status)
    return _go(f"/messages/{res['conversation_id']}", mail, acc)


# ------------------------------------------------------------------ кабинет и страница отклика

def cabinet_summary(db_path: Any, user: dict[str, Any] | None, now: Any) -> dict[str, Any]:
    """Резюме для блока кабинета соискателя (вызывается из шаблонов)."""
    if not user or user.get("role") != "job_seeker":
        return {"ok": False}
    from rabota.accounts import service as accounts

    conn = accounts.open_db(db_path)
    try:
        items = service.list_own(conn, user["id"], now)
    finally:
        conn.close()
    best = max(items, key=lambda r: r["percent"]) if items else None
    return {"ok": True, "items": items, "count": len(items), "limit": rules.MAX_RESUMES,
            "views": sum(r["views"] for r in items), "best": best}


def install(app: FastAPI, *, templates: Any | None = None) -> None:
    """Маршруты, лимиты и функции для шаблонов. Вызывается после install аккаунтов и откликов."""
    acc = getattr(app.state, "accounts", None)
    if acc is None:
        raise RuntimeError("Сначала подключите аккаунты (auth_routes.install)")
    for bucket, limit in LIMITS.items():
        acc.limiter.limits.setdefault(bucket, limit)
    env = getattr(templates, "env", None)
    if env is not None:
        env.globals["cabinet_resumes"] = lambda user: cabinet_summary(acc.db_path, user, acc.now())
    app.include_router(router)
    from rabota.web import admin_resumes

    app.include_router(admin_resumes.router)


__all__ = ["cabinet_summary", "install", "router"]
