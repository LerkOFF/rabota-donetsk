"""Кабинет работодателя: /cabinet/vacancies (список, правка, закрытие, поднятие, удаление) и /cabinet/company.

Подключение: rabota.web.cabinet_routes.install(app, premoderate=...) после аккаунтов. Роль employer (администратору и
соискателю 403), все страницы закрыты от индексации и не кэшируются, все POST проверяют CSRF-токен и Origin,
чужая вакансия всегда 404. Логика и правила в rabota.cabinet.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota import cabinet
from rabota.accounts.deps import Accounts, accounts_of, check_csrf, require_role
from rabota.accounts.forms import read_form
from rabota.cabinet import CabinetError
from rabota.web import queries
from rabota.web.auth_routes import render
from rabota.web.postform import form_options, validate_post

router = APIRouter()
Employer = Annotated[dict[str, Any], Depends(require_role("employer"))]

LIMITS: dict[str, tuple[int, int]] = {"cabinet_user": (120, 3600)}

DONE = {
    "posted": "Вакансия отправлена на проверку. Обычно это занимает несколько часов, после проверки она появится на сайте.",
    "posted_live": "Вакансия опубликована.",
    "saved": "Изменения сохранены.",
    "review": "Изменения сохранены. Вы поменяли оплату, контакты или описание, поэтому вакансия отправлена на повторную "
              "проверку и пока скрыта с сайта.",
    "resubmitted": "Вакансия исправлена и отправлена на проверку снова.",
    "closed": "Вакансия закрыта, соискатели её больше не видят. Открыть её снова можно в любой момент.",
    "raised": "Вакансия поднята, срок размещения начался заново.",
    "reopened": "Вакансия снова открыта.",
    "deleted": "Вакансия удалена.",
}
ERRORS = {
    "too_soon": "Поднимать вакансию можно раз в сутки. Попробуйте позже.",
    "not_allowed": "Так сделать нельзя: вакансия на проверке, отклонена или скрыта модератором.",
    "rate": "Слишком много действий подряд. Подождите немного и повторите.",
}
CABINET_DESCRIPTION = "Личный кабинет работодателя на сайте Работа Донецк: ваши вакансии, отклики и карточка компании."


# ------------------------------------------------------------------ вспомогательное

def _acc(request: Request) -> Accounts:
    acc = accounts_of(request)
    if acc is None:  # pragma: no cover
        raise HTTPException(500, "Аккаунты не подключены")
    return acc


def _vacancy_id(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit() or len(value) > 12:
        raise HTTPException(404)
    return int(value)


async def _db(acc: Accounts, work: Callable[[Any], Any]) -> Any:
    def run() -> Any:
        conn = acc.db()
        try:
            return work(conn)
        finally:
            conn.close()

    return await run_in_threadpool(run)


async def _form(request: Request, user: dict[str, Any], *, limited: bool = True) -> dict[str, str]:
    form = await read_form(request)
    check_csrf(request, form)
    if limited and not _acc(request).limiter.allow("cabinet_user", str(user["id"])):
        raise HTTPException(429)
    return form


def _go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _error(exc: CabinetError) -> HTTPException:
    return HTTPException(404) if exc.code == "not_found" else HTTPException(403, exc.text)


def _view(request: Request, name: str, ctx: dict[str, Any], title: str, description: str = CABINET_DESCRIPTION,
          status: int = 200) -> Response:
    ctx = {**ctx, "seo": {"title": title, "description": description, "noindex": True}}
    return render(request, f"cabinet/{name}", ctx, status)


# ------------------------------------------------------------------ вакансии

def _filters(counts: dict[str, int], state: str) -> list[dict[str, Any]]:
    out = [{"key": "", "label": cabinet.FILTER_LABELS[""], "n": counts["all"], "active": not state}]
    for key in cabinet.STATE_ORDER:
        if counts[key] or key == state:
            out.append({"key": key, "label": cabinet.FILTER_LABELS[key], "n": counts[key], "active": key == state})
    return out


@router.get("/cabinet/vacancies")
async def vacancies_list(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    now = acc.now()
    state = request.query_params.get("state", "")
    state = state if state in cabinet.STATE_ORDER else ""
    page_no = queries.parse_page(request.query_params.get("page"))
    data = await _db(acc, lambda conn: cabinet.list_vacancies(conn, user["id"], now=now, state=state, page=page_no))
    done = request.query_params.get("done", "")
    err = request.query_params.get("err", "")
    return _view(request, "vacancies.html", {
        "data": data, "items": data["items"], "page": data, "state": state, "filters": _filters(data["counts"], state),
        "query": f"state={state}" if state else "",
        "flash": DONE.get(done, ""), "problem": ERRORS.get(err, ""),
    }, "Мои вакансии", "Ваши вакансии на сайте Работа Донецк: статус проверки, отклики, поднятие, правка и закрытие.")


@router.post("/cabinet/vacancies/{vacancy_id}/close")
async def vacancy_close(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)
    await _form(request, user)
    try:
        await _db(acc, lambda conn: cabinet.close_vacancy(conn, user["id"], vid, acc.now()))
    except CabinetError as exc:
        if exc.code == "not_found":
            raise _error(exc) from None
        return _go("/cabinet/vacancies?err=" + exc.code)
    return _go("/cabinet/vacancies?done=closed")


@router.post("/cabinet/vacancies/{vacancy_id}/raise")
async def vacancy_raise(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)
    await _form(request, user)
    try:
        result = await _db(acc, lambda conn: cabinet.raise_vacancy(conn, user["id"], vid, acc.now()))
    except CabinetError as exc:
        if exc.code == "not_found":
            raise _error(exc) from None
        return _go("/cabinet/vacancies?err=" + exc.code)
    return _go("/cabinet/vacancies?done=" + result)


def _edit_context(request: Request, item: dict[str, Any], form: dict[str, str], errors: dict[str, str]) -> dict[str, Any]:
    return {
        **form_options(), "form": form, "errors": errors, "item": item, "form_action": f"/cabinet/vacancies/{item['id']}/edit",
        "with_csrf": True, "submit_label": "Исправить и отправить на проверку" if item["state"].key == "rejected" else "Сохранить изменения", "submit_hint": _edit_hint(item),
    }


def _edit_hint(item: dict[str, Any]) -> str:
    key = item["state"].key
    if key == "rejected":
        return "После отправки вакансия снова попадёт на проверку."
    if key == "pending":
        return "Вакансия уже на проверке: правки сохранятся, проверка не собьётся."
    return ("Если вы измените оплату, контакты или описание, вакансию проверят ещё раз и она на время пропадёт с сайта. "
            "Остальные правки применяются сразу.")


@router.get("/cabinet/vacancies/{vacancy_id}/edit")
async def vacancy_edit_form(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)

    def work(conn: Any) -> dict[str, Any]:
        return cabinet.get_item(conn, user["id"], vid, acc.now())

    try:
        item = await _db(acc, work)
    except CabinetError as exc:
        raise _error(exc) from None
    if not item["can_edit"]:
        raise HTTPException(403, item["state"].note)
    ctx = _edit_context(request, item, cabinet.form_values(item), {})
    return _view(request, "vacancy_edit.html", ctx, "Правка вакансии")


@router.post("/cabinet/vacancies/{vacancy_id}/edit")
async def vacancy_edit_submit(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)
    form = await _form(request, user)
    vacancy, cleaned, errors = validate_post(form)
    premoderate = bool(getattr(request.app.state, "premoderate", True))

    def work(conn: Any) -> tuple[str, dict[str, Any] | None]:
        item = cabinet.get_item(conn, user["id"], vid, acc.now())
        if not item["can_edit"]:
            raise CabinetError("not_allowed", item["state"].note)
        if errors or vacancy is None:
            return "", item
        return cabinet.update_vacancy(conn, user["id"], vid, vacancy, acc.now(), premoderate=premoderate), None

    try:
        outcome, item = await _db(acc, work)
    except CabinetError as exc:
        raise _error(exc) from None
    if item is not None:
        ctx = _edit_context(request, item, cleaned, errors)
        return _view(request, "vacancy_edit.html", ctx, "Правка вакансии", status=422)
    return _go("/cabinet/vacancies?done=" + outcome)


@router.get("/cabinet/vacancies/{vacancy_id}/delete")
async def vacancy_delete_form(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)
    try:
        item = await _db(acc, lambda conn: cabinet.get_item(conn, user["id"], vid, acc.now()))
    except CabinetError as exc:
        raise _error(exc) from None
    return _view(request, "vacancy_delete.html", {"item": item}, "Удалить вакансию")


@router.post("/cabinet/vacancies/{vacancy_id}/delete")
async def vacancy_delete(request: Request, user: Employer, vacancy_id: str) -> Response:
    vid = _vacancy_id(vacancy_id)
    acc = _acc(request)
    await _form(request, user)
    try:
        await _db(acc, lambda conn: cabinet.delete_vacancy(conn, user["id"], vid))
    except CabinetError as exc:
        raise _error(exc) from None
    return _go("/cabinet/vacancies?done=deleted")


# ------------------------------------------------------------------ компания

def _company_page(request: Request, company: dict[str, Any] | None, form: dict[str, str], errors: dict[str, str],
                  status: int = 200) -> Response:
    return _view(request, "company.html", {
        "company": company, "form": form, "errors": errors,
        "flash": "Карточка компании сохранена." if request.query_params.get("saved") == "1" and not errors else "",
        "limits": cabinet.COMPANY_LIMITS,
    }, "Компания", "Карточка вашей компании на сайте Работа Донецк: название, сайт, описание, город, телефон и почта для вакансий.",
        status=status)


@router.get("/cabinet/company")
async def company_form(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    company = await _db(acc, lambda conn: cabinet.get_company(conn, user["id"]))
    form = {k: str((company or {}).get(k) or "") for k in ("name", "site", "description", "city", "phone", "contact_email")}
    form["city"] = form["city"] or "Донецк"
    if not company:
        form["name"] = ""
    return _company_page(request, company, form, {})


@router.post("/cabinet/company")
async def company_save(request: Request, user: Employer) -> Response:
    acc = _acc(request)
    form = await _form(request, user)
    cleaned, errors = cabinet.validate_company(form)
    company = await _db(acc, lambda conn: cabinet.get_company(conn, user["id"]))
    if errors:
        return _company_page(request, company, {**cleaned, "description": cleaned["description"]}, errors, 422)
    await _db(acc, lambda conn: cabinet.save_company(conn, user["id"], cleaned))
    return _go("/cabinet/company?saved=1")


def install(app: FastAPI, *, premoderate: bool = True) -> None:
    """Маршруты кабинета работодателя. Вызывается после install аккаунтов."""
    acc = getattr(app.state, "accounts", None)
    if acc is None:
        raise RuntimeError("Сначала подключите аккаунты (auth_routes.install)")
    for bucket, limit in LIMITS.items():
        acc.limiter.limits.setdefault(bucket, limit)
    app.state.premoderate = premoderate
    app.include_router(router)


__all__ = ["install", "router"]
