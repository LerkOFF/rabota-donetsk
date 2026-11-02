"""Маршруты раздела «Заявки на карточки» админки.

    /admin/claims                 список заявок с фильтрами
    /admin/claims/{id}            карточка заявки: подтвердить, отклонить, запросить пояснения
    /admin/claims/duplicates      похожие карточки и слияние (выбрать главную, перенести вакансии)
    /admin/claims/empty           автокарточки без вакансий

Подключается из rabota.web.admin_routes.install. Все POST проходят admin_form: CSRF, Origin и лимит действий,
каждое действие пишется в журнал.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.accounts.mailer import send_safely
from rabota.admin import claims as admin_claims
from rabota.admin import vacancies
from rabota.employers_auto import claims as claim_service
from rabota.employers_auto import merge
from rabota.web import admin_routes as ar

router = APIRouter()

ar.FLASH.update(admin_claims.FLASH)
vacancies.ACTION_LABELS.update({k: v for k, v in admin_claims.ACTION_LABELS.items() if k not in vacancies.ACTION_LABELS})

DESCRIPTION = "Заявки «Это моя компания»: проверка работодателей, споры за карточки, слияние дублей и пустые автокарточки."
SECTION = "claims"


def _tabs(active: str, counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    return [
        {"key": "claims", "label": "Заявки", "href": "/admin/claims", "n": (counts or {}).get("open"), "active": active == "claims"},
        {"key": "duplicates", "label": "Дубли", "href": "/admin/claims/duplicates", "n": None, "active": active == "duplicates"},
        {"key": "empty", "label": "Без вакансий", "href": "/admin/claims/empty", "n": None, "active": active == "empty"},
    ]


@router.get("/admin/claims")
def claims_list(request: Request, admin: ar.Admin) -> Response:
    st = ar.state_of(request)
    now = st.clock()
    f = admin_claims.parse(request.query_params)
    with ar.dbc(request) as conn:
        page = admin_claims.listing(conn, f, now)
        totals = admin_claims.counts(conn)
    sort_links = {
        key: f.query_without_page(sort=key, dir="asc" if (f.sort == key and f.dir == "desc") else "desc")
        for key in admin_claims.SORTS
    }
    return ar.render(request, "claims.html", {
        "f": f, "page": page, "items": page.items, "totals": totals, "sort_links": sort_links, "tabs": _tabs("claims", totals),
        "options": {"views": admin_claims.VIEWS, "methods": admin_claims.METHODS, "periods": admin_claims.PERIODS,
                    "statuses": admin_claims.STATUSES},
        "query": f.query_without_page(), "full_query": f.query(),
    }, section=SECTION, title="Заявки на карточки", description=DESCRIPTION)


def _claim_page(request: Request, claim_id: int, *, error: str = "", status: int = 200) -> Response:
    with ar.dbc(request) as conn:
        data = admin_claims.card(conn, claim_id)
    if data is None:
        raise HTTPException(404)
    return ar.render(request, "claim.html", {"d": data, "error_text": error, "tabs": _tabs("claims")}, status,
                     section=SECTION, title=f"Заявка №{claim_id}", description=DESCRIPTION)


@router.get("/admin/claims/duplicates")
def duplicates(request: Request, admin: ar.Admin) -> Response:
    with ar.dbc(request) as conn:
        pairs = merge.find_duplicates(conn)
        totals = admin_claims.counts(conn)
    return ar.render(request, "claims_dupes.html", {"pairs": pairs, "tabs": _tabs("duplicates", totals)}, section=SECTION,
                     title="Дубли карточек", description=DESCRIPTION)


@router.get("/admin/claims/empty")
def empty_companies(request: Request, admin: ar.Admin) -> Response:
    view = request.query_params.get("view", "none")
    try:
        page_no = max(int(request.query_params.get("page", "1")), 1)
    except ValueError:
        page_no = 1
    with ar.dbc(request) as conn:
        page = merge.companies_without_vacancies(conn, view, page_no)
        totals = admin_claims.counts(conn)
    view = view if view in merge.EMPTY_VIEWS else "none"
    return ar.render(request, "claims_empty.html", {
        "page": page, "items": page.items, "view": view, "views": merge.EMPTY_VIEWS, "tabs": _tabs("empty", totals),
        "query": f"view={view}" if view != "none" else "",
    }, section=SECTION, title="Компании без вакансий", description=DESCRIPTION)


@router.get("/admin/claims/{claim_id:int}")
def claim_card(request: Request, admin: ar.Admin, claim_id: int) -> Response:
    return _claim_page(request, claim_id)


async def _decide(request: Request, admin: dict[str, Any], claim_id: int, kind: str) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)
    acc = getattr(request.app.state, "accounts", None)

    def work() -> Response:
        with ar.dbc(request) as conn:
            if admin_claims.get(conn, claim_id) is None:
                raise HTTPException(404)
            ip, now = ar.ip_of(request), st.clock()
            site = st.site_url
            try:
                if kind == "approve":
                    outcome = claim_service.approve(conn, claim_id, admin, form.get("note"), ip, site, now,
                                                    transfer=form.get("transfer") == "1")
                elif kind == "reject":
                    outcome = claim_service.reject(conn, claim_id, admin, form.get("reason"), ip, site, now)
                else:
                    outcome = claim_service.request_docs(conn, claim_id, admin, form.get("docs"), ip, site, now)
            except claim_service.ClaimError as exc:
                return _claim_page(request, claim_id, error=exc.text, status=422)
        done = {"approve": "claim_approved", "reject": "claim_rejected", "docs": "claim_docs"}[kind]
        resp = ar.see_other(ar.next_url(form, f"/admin/claims/{claim_id}"), done)
        mails = outcome.mails or ([outcome.mail] if outcome.mail else [])
        if mails and acc is not None:
            def deliver() -> None:
                for mail in mails:
                    send_safely(acc.mailer, mail)

            resp.background = BackgroundTask(deliver)
        return resp

    return await run_in_threadpool(work)


@router.post("/admin/claims/{claim_id:int}/approve")
async def claim_approve(request: Request, admin: ar.Admin, claim_id: int) -> Response:
    return await _decide(request, admin, claim_id, "approve")


@router.post("/admin/claims/{claim_id:int}/reject")
async def claim_reject(request: Request, admin: ar.Admin, claim_id: int) -> Response:
    return await _decide(request, admin, claim_id, "reject")


@router.post("/admin/claims/{claim_id:int}/docs")
async def claim_docs(request: Request, admin: ar.Admin, claim_id: int) -> Response:
    return await _decide(request, admin, claim_id, "docs")


@router.post("/admin/claims/merge")
async def merge_post(request: Request, admin: ar.Admin) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        try:
            main_id, dup_id = int(form.get("main", "0")), int(form.get("dup", "0"))
        except ValueError:
            raise HTTPException(400) from None
        with ar.dbc(request) as conn:
            try:
                merge.merge_companies(conn, main_id, dup_id, admin, ar.ip_of(request), st.clock())
            except merge.MergeError as exc:
                pairs = merge.find_duplicates(conn)
                return ar.render(request, "claims_dupes.html", {"pairs": pairs, "tabs": _tabs("duplicates"), "error_text": exc.text},
                                 422, section=SECTION, title="Дубли карточек", description=DESCRIPTION)
        return ar.see_other("/admin/claims/duplicates", "merged")

    return await run_in_threadpool(work)


@router.post("/admin/claims/empty/delete")
async def empty_delete(request: Request, admin: ar.Admin) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)
    ids = [int(v) for v in form.getlist("ids") if v.isdigit()][:200]

    def work() -> Response:
        with ar.dbc(request) as conn:
            removed = merge.delete_empty(conn, ids, admin, ar.ip_of(request), st.clock())
        return ar.see_other("/admin/claims/empty", "empty_deleted" if removed else "nothing")

    return await run_in_threadpool(work)
