"""Маршруты раздела «Работодатели» админки: список компаний, карточка, проверка и блокировка.

Подключается из rabota.web.admin_routes.install. Все POST проходят admin_form: CSRF, Origin и лимит действий,
каждое действие пишется в журнал (rabota.admin.employers).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.admin import employers, vacancies
from rabota.employers import logo
from rabota.web import admin_routes as ar

router = APIRouter()

ar.FLASH.update(employers.FLASH)
vacancies.ACTION_LABELS.update({
    "vacancy.automod_reject": ("Отклонена автопроверкой", "x-circle"),
    "vacancy.automod_review": ("Автопроверка отправила на ручную проверку", "shield-warning"),
    "vacancy.automod_approve": ("Опубликована автопроверкой (проверенный работодатель)", "seal-check"),
    "employer.verify": ("Работодатель отмечен проверенным", "seal-check"),
    "employer.unverify": ("Отметка о проверке работодателя снята", "seal-check"),
    "employer.block": ("Работодатель заблокирован", "lock-key"),
    "employer.unblock": ("Работодатель разблокирован", "lock-key"),
})

DESCRIPTION = "Компании работодателей: поиск, фильтры, отметка «проверенный работодатель», блокировка и история действий."


@router.get("/admin/employers")
def employers_list(request: Request, admin: ar.Admin) -> Response:
    st = ar.state_of(request)
    now = st.clock()
    f = employers.parse(request.query_params)
    with ar.dbc(request) as conn:
        page = employers.listing(conn, f, now)
        totals = employers.counts(conn)
    sort_links = {
        key: f.query_without_page(sort=key, dir="asc" if (f.sort == key and f.dir == "desc") else "desc")
        for key in employers.SORTS
    }
    return ar.render(request, "employers.html", {
        "f": f, "page": page, "items": page.items, "totals": totals, "sort_links": sort_links,
        "options": {"verified": employers.VERIFIED, "states": employers.STATES, "periods": employers.PERIODS,
                    "sorts": employers.SORTS},
        "query": f.query_without_page(), "full_query": f.query(),
    }, section="employers", title="Работодатели", description=DESCRIPTION)


def _card(request: Request, company_id: int, **extra: Any) -> dict[str, Any]:
    st = ar.state_of(request)
    with ar.dbc(request) as conn:
        data = employers.card(conn, company_id, st.clock())
    if data is None:
        raise HTTPException(404)
    data["logo_url"] = logo.logo_url(data["company"])
    data.update(extra)
    return data


@router.get("/admin/employers/{company_id}")
def employer_card(request: Request, admin: ar.Admin, company_id: int) -> Response:
    data = _card(request, company_id)
    name = data["company"]["name"]
    return ar.render(request, "employer.html", {"d": data, "error_text": ""}, section="employers",
                     title=f"Работодатель: {name}", description=DESCRIPTION)


async def _action(request: Request, admin: dict[str, Any], company_id: int, kind: str) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        with ar.dbc(request) as conn:
            if employers.get(conn, company_id) is None:
                raise HTTPException(404)
            ip, now, note = ar.ip_of(request), st.clock(), form.get("note")
            try:
                if kind == "verify":
                    employers.set_verified(conn, company_id, admin, note, ip, now)
                elif kind == "unverify":
                    employers.clear_verified(conn, company_id, admin, note, ip, now)
                elif kind == "block":
                    employers.block(conn, company_id, admin, form.get("reason"), ip, now)
                else:
                    employers.unblock(conn, company_id, admin, ip, now)
            except ValueError as exc:
                data = _card(request, company_id)
                return ar.render(request, "employer.html", {"d": data, "error_text": str(exc)}, 422, section="employers",
                                 title=f"Работодатель: {data['company']['name']}", description=DESCRIPTION)
        done = {"verify": "employer_verified", "unverify": "employer_unverified", "block": "employer_blocked",
                "unblock": "employer_unblocked"}[kind]
        return ar.see_other(ar.next_url(form, f"/admin/employers/{company_id}"), done)

    return await run_in_threadpool(work)


@router.post("/admin/employers/{company_id}/verify")
async def employer_verify(request: Request, admin: ar.Admin, company_id: int) -> Response:
    return await _action(request, admin, company_id, "verify")


@router.post("/admin/employers/{company_id}/unverify")
async def employer_unverify(request: Request, admin: ar.Admin, company_id: int) -> Response:
    return await _action(request, admin, company_id, "unverify")


@router.post("/admin/employers/{company_id}/block")
async def employer_block(request: Request, admin: ar.Admin, company_id: int) -> Response:
    return await _action(request, admin, company_id, "block")


@router.post("/admin/employers/{company_id}/unblock")
async def employer_unblock(request: Request, admin: ar.Admin, company_id: int) -> Response:
    return await _action(request, admin, company_id, "unblock")
