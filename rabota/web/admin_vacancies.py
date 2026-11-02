"""Маршруты раздела «Вакансии» админки: список с фильтрами и действие «повтор».

Подключается из rabota.web.admin_routes.install. Карточка вакансии (`GET /admin/vacancies/{id}`), скрытие и возврат
живут в admin_routes и модерации; здесь только список и отметка повторяющейся. Все POST проходят admin_form:
CSRF, Origin и лимит действий.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota.admin import vacancies
from rabota.web import admin_routes as ar

router = APIRouter()

ar.FLASH.update(vacancies.FLASH)

DESCRIPTION = "Все вакансии базы: скрытые, просроченные, дубли и чужих городов. Фильтры, поиск и переход в карточку с историей."


@router.get("/admin/vacancies")
def vacancies_list(request: Request, admin: ar.Admin) -> Response:
    from rabota.web.app import present_vacancy

    st = ar.state_of(request)
    now = st.clock()
    f = vacancies.parse(request.query_params)
    with ar.dbc(request) as conn:
        page = vacancies.listing(conn, f, now)
        totals = vacancies.counts(conn, now)
        facets = vacancies.facets(conn)
    items: list[dict[str, Any]] = []
    for v in page.items:
        pv = present_vacancy(v, now)
        items.append({**v, "pv": pv, "contact_items": vacancies.contact_items(v["contacts"])})
    sort_links = {
        key: f.query_without_page(sort=key, dir="asc" if (f.sort == key and f.dir == "desc") else "desc")
        for key in vacancies.SORTS
    }
    return ar.render(request, "vacancies.html", {
        "f": f, "page": page, "items": items, "totals": totals, "facets": facets, "sort_links": sort_links,
        "options": {
            "statuses": vacancies.STATUSES, "moderation": vacancies.MODERATION, "origins": vacancies.ORIGINS,
            "flags": vacancies.FLAGS, "kinds": vacancies.KINDS, "periods": vacancies.PERIODS, "sorts": vacancies.SORTS,
            "foreign_city": vacancies.FOREIGN_CITY,
        },
        "query": f.query_without_page(), "full_query": f.query(),
    }, section="vacancies", title="Вакансии", description=DESCRIPTION)


@router.post("/admin/vacancies/{vacancy_id}/duplicate")
async def vacancy_duplicate(request: Request, admin: ar.Admin, vacancy_id: int) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        ar._exists(request, vacancy_id)
        raw = form.get("main_id").strip().lstrip("№#")
        if not raw.isdigit() or len(raw) > 9:
            return ar.vacancy_error(request, vacancy_id, "Укажите номер главной вакансии цифрами.")
        with ar.dbc(request) as conn:
            try:
                vacancies.mark_duplicate(conn, vacancy_id, int(raw), admin, ar.ip_of(request), st.clock())
            except ValueError as exc:
                return ar.vacancy_error(request, vacancy_id, str(exc))
        return ar.see_other(ar.next_url(form, f"/admin/vacancies/{vacancy_id}"), "duplicate_marked")

    return await run_in_threadpool(work)


@router.post("/admin/vacancies/{vacancy_id}/unduplicate")
async def vacancy_unduplicate(request: Request, admin: ar.Admin, vacancy_id: int) -> Response:
    form = await ar.admin_form(request, admin)
    st = ar.state_of(request)

    def work() -> Response:
        ar._exists(request, vacancy_id)
        with ar.dbc(request) as conn:
            if not vacancies.clear_duplicate(conn, vacancy_id, admin, ar.ip_of(request), st.clock()):
                raise HTTPException(404)
        return ar.see_other(ar.next_url(form, f"/admin/vacancies/{vacancy_id}"), "duplicate_cleared")

    return await run_in_threadpool(work)


def card_extras(conn: Any, v: dict[str, Any], now: Any) -> dict[str, Any]:
    """Дополнение контекста карточки: история, исходный пост, связанные записи, закрытые контакты."""
    return vacancies.card_extras(conn, v, now)
