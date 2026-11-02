"""Раздел «Резюме» админки: список с фильтрами, карточка, скрытие нарушающих и возврат.

Подключается из rabota.web.resume_routes.install. Все POST проходят admin_form: CSRF, Origin и лимит действий,
каждое действие пишется в журнал (rabota.admin.audit).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.resumes import admin as resumes_admin
from rabota.web import admin_routes as ar

router = APIRouter()

ar.FLASH.update(resumes_admin.FLASH)

DESCRIPTION = (
    "Все резюме соискателей: кому показаны, давно не обновлялись, скрытые. Просмотры, открытые контакты и скрытие нарушающих."
)


@router.get("/admin/resumes")
def resumes_list(request: Request, admin: ar.Admin) -> Response:
    st = ar.state_of(request)
    now = st.clock()
    f = resumes_admin.parse(request.query_params)
    with ar.dbc(request) as conn:
        page = resumes_admin.listing(conn, f, now)
        totals = resumes_admin.counts(conn, now)
    return ar.render(request, "resumes.html", {
        "f": f, "page": page, "items": page.items, "totals": totals, "views": resumes_admin.VIEWS,
        "query": resumes_admin.query_string(f),
    }, section="resumes", title="Резюме", description=DESCRIPTION)


def _card(request: Request, resume_id: int, *, error: str = "", status: int = 200) -> Response:
    now = ar.state_of(request).clock()
    with ar.dbc(request) as conn:
        resume = resumes_admin.get(conn, resume_id, now)
    if resume is None:
        raise HTTPException(404)
    return ar.render(request, "resume.html", {"r": resume, "error": error}, status, section="resumes",
                     title=f"Резюме №{resume_id}", description="Карточка резюме: данные, согласие, просмотры, скрытие.")


@router.get("/admin/resumes/{resume_id}")
def resume_page(request: Request, admin: ar.Admin, resume_id: int) -> Response:
    return _card(request, resume_id)


@router.post("/admin/resumes/{resume_id}/hide")
async def resume_hide(request: Request, admin: ar.Admin, resume_id: int) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        with ar.dbc(request) as conn:
            try:
                found = resumes_admin.hide(conn, resume_id, form.get("note"), admin, ar.ip_of(request), st.clock())
            except ValueError as exc:
                return _card(request, resume_id, error=str(exc), status=422)
        if not found:
            raise HTTPException(404)
        return ar.see_other(ar.next_url(form, f"/admin/resumes/{resume_id}"), "resume_hidden")

    return await run_in_threadpool(work)


@router.post("/admin/resumes/{resume_id}/restore")
async def resume_restore(request: Request, admin: ar.Admin, resume_id: int) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        with ar.dbc(request) as conn:
            if not resumes_admin.restore(conn, resume_id, admin, ar.ip_of(request), st.clock()):
                raise HTTPException(404)
        return ar.see_other(ar.next_url(form, f"/admin/resumes/{resume_id}"), "resume_restored")

    return await run_in_threadpool(work)

