"""Админка (/admin) и публичная форма запросов по персональным данным (/legal/request).

    from rabota.web import admin_routes
    admin_routes.install(app, db_path=..., site_url=..., clock=...)

Доступ: только вошедший пользователь с ролью admin. Гость уходит на вход, остальные получают 404, повторные попытки
ограничены. Каждый POST проверяет токен CSRF и Origin. Все действия пишутся в журнал (rabota.admin.audit).
Страницы закрыты от индекса (meta robots, X-Robots-Tag, robots.txt) и не кешируются.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from rabota import config
from rabota.accounts import service
from rabota.accounts.deps import check_csrf, client_ip, csrf_token, get_current_user, require_user
from rabota.accounts.mailer import SITE_NAME, Mail, send_safely
from rabota.accounts.ratelimit import RateLimiter
from rabota.admin import audit, complaints, dashboard, moderation, stoplist
from rabota.admin import requests as legal_requests
from rabota.admin.common import clean_line, iso
from rabota.lifecycle import parse_iso
from rabota.models import CATEGORIES, EMPLOYMENTS, PAY_PERIODS, WORK_FORMATS
from rabota.realtime.health import human_age
from rabota.web import format as fmt
from rabota.web import seo as seolib

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
MAX_BODY = 48 * 1024
LOCAL_UTC_OFFSET = timedelta(hours=3)  # Донецк, московское время

DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "admin_denied": (10, 900),   # попыток зайти в админку без прав с одного адреса за 15 минут
    "admin_post": (240, 60),     # действий администратора в минуту
    "legal_request": (4, 3600),  # обращений с одного адреса в час
}

FLASH = {
    "approved": "Вакансия одобрена и опубликована.",
    "rejected": "Вакансия отклонена, автору отправлено письмо.",
    "rejected_nomail": "Вакансия отклонена. Письмо автору не отправлено: адрес не указан или почта недоступна.",
    "hidden": "Вакансия скрыта.",
    "restored": "Вакансия возвращена в выдачу.",
    "edited": "Изменения сохранены.",
    "nochange": "Ничего не изменилось.",
    "complaint_hidden": "Вакансия скрыта, жалобы закрыты.",
    "complaint_stoplist": "Записи добавлены в стоп-лист, вакансия скрыта, жалобы закрыты.",
    "complaint_dismissed": "Жалоба отклонена.",
    "replied": "Ответ отправлен заявителю.",
    "reply_failed": "Письмо не ушло: почтовый сервер недоступен. Попробуйте позже.",
    "report_done": "Жалоба обработана.",
    "stop_added": "Запись добавлена в стоп-лист.",
    "stop_exists": "Такая запись уже есть в стоп-листе.",
    "stop_removed": "Запись удалена из стоп-листа.",
    "request_created": "Обращение записано в журнал.",
    "request_updated": "Обращение обновлено.",
    "erased": "Данные удалены: вакансии скрыты, контакт убран и занесён в стоп-лист.",
}

MENU = (
    ("dashboard", "Сводка", "/admin", "gear-six"),
    ("moderation", "Модерация", "/admin/moderation", "seal-check"),
    ("complaints", "Жалобы", "/admin/complaints", "flag"),
    ("requests", "Запросы по ПДн", "/admin/requests", "identification-card"),
    ("stoplist", "Стоп-лист", "/admin/stoplist", "shield-warning"),
    ("audit", "Журнал действий", "/admin/audit", "file-text"),
)


@dataclass
class AdminState:
    db_path: Path | str
    site_url: str
    clock: Callable[[], datetime]
    limiter: RateLimiter
    health: Callable[[sqlite3.Connection], Any] | None = None
    templates: Jinja2Templates | None = None
    extra: dict[str, Any] = field(default_factory=dict)


router = APIRouter()


def state_of(request: Request) -> AdminState:
    return request.app.state.admin  # type: ignore[no-any-return]


def secret_of(request: Request) -> bytes:
    acc = getattr(request.app.state, "accounts", None)
    return acc.secret if acc is not None else b"rabota-admin-fallback-secret-key-0000"


@contextmanager
def dbc(request: Request):
    conn = service.open_db(state_of(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


def ip_of(request: Request) -> str:
    return client_ip(request)


# ------------------------------------------------------------------ доступ

def require_admin(request: Request) -> dict[str, Any]:
    """Только администратор. Гость уходит на вход, остальным страницы «нет» (404), попытки считаются."""
    user = require_user(request)
    if user.get("role") != "admin" or not user.get("verified"):
        limiter = state_of(request).limiter
        if not limiter.allow("admin_denied", ip_of(request)):
            raise HTTPException(429)
        raise HTTPException(404)
    return user


Admin = Annotated[dict[str, Any], Depends(require_admin)]


class Form:
    """Поля формы: get - первое значение, getlist - все."""

    def __init__(self, data: dict[str, list[str]]):
        self.data = data

    def get(self, name: str, default: str = "") -> str:
        values = self.data.get(name)
        return values[0] if values else default

    def getlist(self, name: str) -> list[str]:
        return list(self.data.get(name, []))

    def first_values(self) -> dict[str, str]:
        return {k: v[0] for k, v in self.data.items() if v}

    def has(self, name: str) -> bool:
        return name in self.data


async def read_form(request: Request) -> Form:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Слишком большой запрос")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=120)
    return Form(parsed)


async def admin_form(request: Request, admin: dict[str, Any]) -> Form:
    """Тело POST-формы после проверок: CSRF, Origin, частота действий этого администратора."""
    form = await read_form(request)
    check_csrf(request, form.first_values())
    if not state_of(request).limiter.allow("admin_post", str(admin["id"])):
        raise HTTPException(429)
    return form


# ------------------------------------------------------------------ шаблоны

def build_templates(site_url: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env

    def asset(name: str) -> str:
        try:
            version = int((STATIC_DIR / name).stat().st_mtime)
        except OSError:
            version = 0
        return f"/static/{name}?v={version}"

    def local(value: str | None) -> datetime | None:
        dt = parse_iso(value)
        return None if dt is None else dt + LOCAL_UTC_OFFSET

    env.globals.update(
        asset=asset, site_url=site_url, pay_parts=fmt.pay_parts,
        category_icon=lambda c: fmt.CATEGORY_ICONS.get(c, "dots-three-circle"),
        category_label=lambda c: fmt.CATEGORY_LABELS.get(c, str(c).capitalize()),
        work_format_label=lambda c: fmt.WORK_FORMAT_LABELS.get(c, c), count_label=fmt.count_label, plural=fmt.plural,
        money=fmt.money, district_label=fmt.district_label, phone_pretty=fmt.phone_pretty, phone_href=fmt.phone_href,
        telegram_href=fmt.telegram_href, human_age=human_age,
        search_hints=lambda: {"professions": [], "cities": [], "districts": []},
    )
    env.filters["dt"] = lambda v: (local(v).strftime("%d.%m.%Y %H:%M") if local(v) else "")
    env.filters["date"] = lambda v: (local(v).strftime("%d.%m.%Y") if local(v) else "")
    env.filters["ucfirst"] = lambda s: (s[:1].upper() + s[1:]) if s else s
    env.filters["quote"] = lambda s: quote(str(s), safe="")
    return templates


def nav_counts(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    mod = moderation.counts(conn)
    cmp_ = complaints.counts(conn)
    req = legal_requests.counts(conn, now)
    return {
        "moderation": mod["pending"], "complaints": cmp_["open"] + cmp_["reports_open"],
        "requests": req["open"], "requests_overdue": req["overdue"],
    }


def render(request: Request, name: str, context: dict[str, Any] | None = None, status: int = 200, *, section: str = "dashboard",
           title: str = "", description: str = "") -> Response:
    st = state_of(request)
    user = get_current_user(request)
    with dbc(request) as conn:
        nav = nav_counts(conn, st.clock())
    seo = seolib.build_seo(st.site_url, request.url.path, title=title or "Админка", description=description, noindex=True)
    seo["description"] = " ".join(description.split()) or "Служебная часть сайта: модерация, жалобы и запросы."
    seo["robots"] = "noindex, nofollow"
    ctx: dict[str, Any] = {
        "request": request, "seo": seo, "me": user, "csrf_token": csrf_token(request), "nav": nav, "menu": MENU,
        "section": section, "path": request.url.path, "flash": FLASH.get(request.query_params.get("ok", "")),
        "now": st.clock(), "page_title": title,
    }
    ctx.update(context or {})
    response = st.templates.TemplateResponse(request, f"admin/{name}", ctx, status_code=status)  # type: ignore[union-attr]
    return response


def see_other(url: str, ok: str = "") -> RedirectResponse:
    if ok:
        url += ("&" if "?" in url else "?") + urlencode({"ok": ok})
    return RedirectResponse(url, status_code=303)


def next_url(form: Form, default: str) -> str:
    """Куда вернуться после действия: только внутренние адреса админки."""
    target = form.get("next")
    if target.startswith("/admin") and not target.startswith("//") and "\\" not in target and "\n" not in target:
        return target
    return default


def page_no(request: Request) -> int:
    try:
        return min(max(int(request.query_params.get("page", "1")), 1), 100_000)
    except ValueError:
        return 1


def view_of(request: Request, allowed: dict[str, Any], default: str) -> str:
    view = request.query_params.get("view", default)
    return view if view in allowed else default


# ------------------------------------------------------------------ сводка

@router.get("/admin")
def dashboard_page(request: Request, admin: Admin) -> Response:
    st = state_of(request)
    with dbc(request) as conn:
        data = dashboard.build(conn, st.clock(), st.health)
    return render(request, "dashboard.html", {"d": data}, section="dashboard", title="Сводка",
                  description="Что требует внимания: вакансии на проверке, жалобы, запросы по данным и состояние источников.")


# ------------------------------------------------------------------ модерация вакансий

def vacancy_context(conn: sqlite3.Connection, request: Request, vacancy_id: int) -> dict[str, Any]:
    from rabota.web.app import present_vacancy

    v = moderation.get(conn, vacancy_id)
    if v is None:
        raise HTTPException(404)
    st = state_of(request)
    owner = None
    if v.get("owner_user_id"):
        owner = conn.execute("SELECT id, email, name FROM users WHERE id=?", (v["owner_user_id"],)).fetchone()
    contacts = v["contacts"] or {}
    return {
        "v": v, "pv": present_vacancy(v, st.clock()), "owner": dict(owner) if owner else None,
        "match": moderation.stoplist_match(conn, vacancy_id),
        "complaints": complaints.for_vacancy(conn, vacancy_id),
        "reject_templates": [(k, moderation.REJECT_LABELS[k], t) for k, t in moderation.REJECT_TEMPLATES.items()],
        "edit": edit_defaults(v, contacts),
        "choices": {
            "categories": [(c, fmt.CATEGORY_LABELS[c]) for c in CATEGORIES],
            "work_formats": [(w, fmt.WORK_FORMAT_LABELS[w]) for w in WORK_FORMATS],
            "employments": [(e, fmt.EMPLOYMENT_LABELS[e]) for e in EMPLOYMENTS],
            "periods": [(p, fmt.PERIOD_LABELS[p]) for p in PAY_PERIODS],
        },
        "errors": {}, "error": "", "notify": moderation.notify_address(conn, vacancy_id),
        "ttl_days": config.VACANCY_TTL_DAYS,
    }


def edit_defaults(v: dict[str, Any], contacts: dict[str, Any]) -> dict[str, str]:
    phones = contacts.get("phones") or []
    return {
        "profession": v["profession"] or "", "category": v["category"] or "", "work_format": v["work_format"] or "",
        "employment": v["employment"] or "", "schedule": v["schedule"] or "",
        "pay_min": str(v["pay_min"] or ""), "pay_max": str(v["pay_max"] or ""), "pay_period": v["pay_period"] or "",
        "pay_daily": "1" if v["pay_daily"] else "", "no_experience": "1" if (v["experience"] or "").startswith("не нужен") else "",
        "city": v["city"] or "", "district": v["district"] or "", "age_min": str(v["age_min"] or ""),
        "requirements": v["requirements"] or "", "employer": v["employer"] or "", "summary": v["summary"] or "",
        "phone": phones[0] if phones else "", "telegram": contacts.get("telegram") or "", "email": contacts.get("email") or "",
    }


@router.get("/admin/moderation")
def moderation_list(request: Request, admin: Admin) -> Response:
    view = view_of(request, moderation.VIEWS, "pending")
    q = request.query_params.get("q", "")
    with dbc(request) as conn:
        page = moderation.queue(conn, view, page_no(request), q)
        counts = moderation.counts(conn)
        items = [
            {**v, "stop": moderation.stoplist_match(conn, v["id"]), "complaints": conn.execute(
                "SELECT COUNT(*) FROM complaints WHERE vacancy_id=? AND status='new'", (v["id"],)).fetchone()[0]}
            for v in page.items
        ]
    st = state_of(request)
    from rabota.web.app import present_vacancy

    for item in items:
        item["pv"] = present_vacancy(item, st.clock())
    return render(request, "moderation.html", {
        "view": view, "views": moderation.VIEWS, "page": page, "items": items, "counts": counts, "q": q,
    }, section="moderation", title="Модерация вакансий",
        description="Вакансии работодателей на проверке: просмотр, одобрение, отклонение с причиной и правка полей.")


@router.get("/admin/vacancies/{vacancy_id}")
def vacancy_page(request: Request, admin: Admin, vacancy_id: int) -> Response:
    with dbc(request) as conn:
        ctx = vacancy_context(conn, request, vacancy_id)
    return render(request, "vacancy.html", ctx, section="moderation", title=f"Вакансия {vacancy_id}",
                  description="Карточка вакансии для проверки: как её увидит посетитель, действия модератора и правка полей.")


def vacancy_error(request: Request, vacancy_id: int, message: str, errors: dict[str, str] | None = None,
                  form: dict[str, str] | None = None) -> Response:
    with dbc(request) as conn:
        ctx = vacancy_context(conn, request, vacancy_id)
    ctx.update({"error": message, "errors": errors or {}})
    if form is not None:
        ctx["edit"] = form
    return render(request, "vacancy.html", ctx, 422, section="moderation", title=f"Вакансия {vacancy_id}",
                  description="Карточка вакансии для проверки: как её увидит посетитель, действия модератора и правка полей.")


def _exists(request: Request, vacancy_id: int) -> None:
    with dbc(request) as conn:
        if moderation.get(conn, vacancy_id) is None:
            raise HTTPException(404)


@router.post("/admin/vacancies/{vacancy_id}/approve")
async def vacancy_approve(request: Request, admin: Admin, vacancy_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _exists(request, vacancy_id)
        with dbc(request) as conn:
            moderation.approve(conn, vacancy_id, admin, state_of(request).clock())
        return see_other(next_url(form, "/admin/moderation"), "approved")

    return await run_in_threadpool(work)


@router.post("/admin/vacancies/{vacancy_id}/reject")
async def vacancy_reject(request: Request, admin: Admin, vacancy_id: int) -> Response:
    form = await admin_form(request, admin)
    st = state_of(request)

    def work() -> Response:
        _exists(request, vacancy_id)
        reason = moderation.reason_text(form.get("reason_code"), form.get("reason"))
        with dbc(request) as conn:
            try:
                moderation.reject(conn, vacancy_id, admin, reason, st.clock())
            except ValueError as exc:
                return vacancy_error(request, vacancy_id, str(exc))
            address = moderation.notify_address(conn, vacancy_id)
        sent = False
        if address:
            body = (
                f"Здравствуйте!\n\nВакансия «{_title_of(request, vacancy_id)}» не прошла проверку на сайте {SITE_NAME}.\n\n"
                f"Причина: {reason}\n\nИсправьте вакансию и отправьте её снова: {st.site_url}/razmestit\n\n{SITE_NAME}\n{st.site_url}\n"
            )
            acc = request.app.state.accounts
            sent = send_safely(acc.mailer, Mail(address, f"Вакансия не прошла проверку: {SITE_NAME}", body))
        return see_other(next_url(form, "/admin/moderation"), "rejected" if sent else "rejected_nomail")

    return await run_in_threadpool(work)


def _title_of(request: Request, vacancy_id: int) -> str:
    with dbc(request) as conn:
        v = moderation.get(conn, vacancy_id)
    return (v["profession"] if v else "").capitalize()


@router.post("/admin/vacancies/{vacancy_id}/hide")
async def vacancy_hide(request: Request, admin: Admin, vacancy_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _exists(request, vacancy_id)
        with dbc(request) as conn:
            moderation.hide(conn, vacancy_id, admin, form.get("reason"), state_of(request).clock())
        return see_other(next_url(form, f"/admin/vacancies/{vacancy_id}"), "hidden")

    return await run_in_threadpool(work)


@router.post("/admin/vacancies/{vacancy_id}/restore")
async def vacancy_restore(request: Request, admin: Admin, vacancy_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _exists(request, vacancy_id)
        with dbc(request) as conn:
            ok = moderation.restore(conn, vacancy_id, admin, state_of(request).clock())
        if not ok:
            return vacancy_error(request, vacancy_id, "Вернуть можно только скрытую вакансию, прошедшую проверку. "
                                                      "Непроверенную сначала одобрите.")
        return see_other(next_url(form, f"/admin/vacancies/{vacancy_id}"), "restored")

    return await run_in_threadpool(work)


@router.post("/admin/vacancies/{vacancy_id}/edit")
async def vacancy_edit(request: Request, admin: Admin, vacancy_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        from rabota.web.app import validate_post

        _exists(request, vacancy_id)
        vacancy, cleaned, errors = validate_post(form.first_values())
        if errors or vacancy is None:
            return vacancy_error(request, vacancy_id, "Нужно поправить поля формы.", errors, cleaned)
        with dbc(request) as conn:
            old = moderation.get(conn, vacancy_id) or {}
            contacts = dict(old.get("contacts") or {})
            for key in ("phones", "telegram", "email"):
                contacts.pop(key, None)
            contacts.update(vacancy.contacts)
            experience = vacancy.experience or (None if (old.get("experience") or "").startswith("не нужен") else old.get("experience"))
            values = {
                "profession": vacancy.profession, "title_raw": old.get("title_raw") or vacancy.title_raw,
                "category": vacancy.category, "work_format": vacancy.work_format, "employment": vacancy.employment,
                "schedule": vacancy.schedule, "pay_min": vacancy.pay_min, "pay_max": vacancy.pay_max,
                "pay_period": vacancy.pay_period, "pay_daily": vacancy.pay_daily, "city": vacancy.city,
                "district": vacancy.district, "experience": experience, "age_min": vacancy.age_min,
                "requirements": vacancy.requirements, "employer": vacancy.employer, "summary": vacancy.summary,
                "contacts": contacts,
            }
            changed = moderation.update_fields(conn, vacancy_id, admin, values, state_of(request).clock())
        return see_other(f"/admin/vacancies/{vacancy_id}", "edited" if changed else "nochange")

    return await run_in_threadpool(work)


# ------------------------------------------------------------------ жалобы

@router.get("/admin/complaints")
def complaints_list(request: Request, admin: Admin) -> Response:
    view = view_of(request, complaints.VIEWS, "open")
    with dbc(request) as conn:
        page = complaints.queue(conn, view, page_no(request))
        counts = complaints.counts(conn)
        threshold = complaints.autohide_threshold()
    return render(request, "complaints.html", {
        "view": view, "views": complaints.VIEWS, "page": page, "counts": counts, "threshold": threshold,
        "tab": "vacancies",
    }, section="complaints", title="Жалобы на вакансии",
        description="Очередь жалоб на вакансии: причины, число жалоб на одну вакансию, действия модератора и ответы заявителям.")


@router.get("/admin/reports")
def reports_list(request: Request, admin: Admin) -> Response:
    view = view_of(request, complaints.VIEWS, "open")
    with dbc(request) as conn:
        page = complaints.reports_queue(conn, view, page_no(request))
        counts = complaints.counts(conn)
    return render(request, "reports.html", {
        "view": view, "views": {k: v for k, v in complaints.VIEWS.items() if k != "all"}, "page": page, "counts": counts,
        "tab": "reports",
    }, section="complaints", title="Жалобы на сообщения и людей",
        description="Жалобы на сообщения, пользователей и резюме: просмотр, отклонение и блокировка нарушителя.")


def complaint_context(conn: sqlite3.Connection, request: Request, complaint_id: int) -> dict[str, Any]:
    c = complaints.get(conn, complaint_id)
    if c is None:
        raise HTTPException(404)
    vacancy = moderation.get(conn, c["vacancy_id"])
    pv = None
    if vacancy:
        from rabota.web.app import present_vacancy

        pv = present_vacancy(vacancy, state_of(request).clock())
    return {
        "c": c, "v": vacancy, "pv": pv, "others": [x for x in complaints.for_vacancy(conn, c["vacancy_id"]) if x["id"] != c["id"]],
        "candidates": complaints.stop_candidates(conn, c["vacancy_id"]), "error": "",
        "stop_kinds": list(stoplist.KINDS.items()), "threshold": complaints.autohide_threshold(),
    }


@router.get("/admin/complaints/{complaint_id}")
def complaint_page(request: Request, admin: Admin, complaint_id: int) -> Response:
    with dbc(request) as conn:
        ctx = complaint_context(conn, request, complaint_id)
    return render(request, "complaint.html", ctx, section="complaints", title=f"Жалоба {complaint_id}",
                  description="Жалоба на вакансию: что написал заявитель, вакансия, другие жалобы и действия модератора.")


def complaint_error(request: Request, complaint_id: int, message: str) -> Response:
    with dbc(request) as conn:
        ctx = complaint_context(conn, request, complaint_id)
    ctx["error"] = message
    return render(request, "complaint.html", ctx, 422, section="complaints", title=f"Жалоба {complaint_id}",
                  description="Жалоба на вакансию: что написал заявитель, вакансия, другие жалобы и действия модератора.")


def _complaint_exists(request: Request, complaint_id: int) -> None:
    with dbc(request) as conn:
        if complaints.get(conn, complaint_id) is None:
            raise HTTPException(404)


@router.post("/admin/complaints/{complaint_id}/hide")
async def complaint_hide(request: Request, admin: Admin, complaint_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _complaint_exists(request, complaint_id)
        with dbc(request) as conn:
            complaints.resolve(conn, complaint_id, "hide", admin, now=state_of(request).clock(), note=form.get("note"))
        return see_other(next_url(form, "/admin/complaints"), "complaint_hidden")

    return await run_in_threadpool(work)


@router.post("/admin/complaints/{complaint_id}/stoplist")
async def complaint_stoplist(request: Request, admin: Admin, complaint_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _complaint_exists(request, complaint_id)
        entries: list[tuple[str, str]] = []
        for item in form.getlist("stop"):
            kind, _, value = item.partition("|")
            entries.append((kind, value))
        if form.get("extra_value").strip():
            entries.append((form.get("extra_kind") or "keyword", form.get("extra_value")))
        with dbc(request) as conn:
            try:
                complaints.resolve(conn, complaint_id, "stoplist", admin, now=state_of(request).clock(),
                                   note=form.get("note"), stop=entries)
            except ValueError as exc:
                return complaint_error(request, complaint_id, str(exc))
        return see_other(next_url(form, "/admin/complaints"), "complaint_stoplist")

    return await run_in_threadpool(work)


@router.post("/admin/complaints/{complaint_id}/dismiss")
async def complaint_dismiss(request: Request, admin: Admin, complaint_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        _complaint_exists(request, complaint_id)
        with dbc(request) as conn:
            complaints.resolve(conn, complaint_id, "dismiss", admin, now=state_of(request).clock(),
                               note=form.get("note"), restore=bool(form.get("restore")))
        return see_other(next_url(form, "/admin/complaints"), "complaint_dismissed")

    return await run_in_threadpool(work)


@router.post("/admin/complaints/{complaint_id}/reply")
async def complaint_reply(request: Request, admin: Admin, complaint_id: int) -> Response:
    form = await admin_form(request, admin)
    st = state_of(request)

    def work() -> Response:
        _complaint_exists(request, complaint_id)
        with dbc(request) as conn:
            try:
                mail = complaints.build_reply(conn, complaint_id, form.get("text"), st.site_url)
            except ValueError as exc:
                return complaint_error(request, complaint_id, str(exc))
            if not send_safely(request.app.state.accounts.mailer, mail):
                return see_other(f"/admin/complaints/{complaint_id}", "reply_failed")
            complaints.mark_replied(conn, complaint_id, admin, st.clock())
        return see_other(f"/admin/complaints/{complaint_id}", "replied")

    return await run_in_threadpool(work)


@router.post("/admin/reports/{report_id}/{action}")
async def report_action(request: Request, admin: Admin, report_id: int, action: str) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        if action not in complaints.REPORT_ACTIONS:
            raise HTTPException(404)
        with dbc(request) as conn:
            if conn.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone() is None:
                raise HTTPException(404)
            try:
                complaints.report_resolve(conn, report_id, action, admin, state_of(request).clock(), form.get("note"))
            except ValueError as exc:
                page = complaints.reports_queue(conn, "open", 1)
                return render(request, "reports.html", {
                    "view": "open", "views": {"open": "Открытые", "closed": "Закрытые"}, "page": page,
                    "counts": complaints.counts(conn), "tab": "reports", "error": str(exc),
                }, 422, section="complaints", title="Жалобы на сообщения и людей",
                    description="Жалобы на сообщения, пользователей и резюме: просмотр, отклонение и блокировка нарушителя.")
        return see_other("/admin/reports", "report_done")

    return await run_in_threadpool(work)


# ------------------------------------------------------------------ стоп-лист

def stoplist_page_ctx(conn: sqlite3.Connection, request: Request, **extra: Any) -> dict[str, Any]:
    kind = request.query_params.get("kind", "")
    return {
        "entries": stoplist.listing(conn, kind), "kinds": list(stoplist.KINDS.items()), "kind": kind,
        "form": {}, "error": "", **extra,
    }


@router.get("/admin/stoplist")
def stoplist_list(request: Request, admin: Admin) -> Response:
    with dbc(request) as conn:
        ctx = stoplist_page_ctx(conn, request)
    return render(request, "stoplist.html", ctx, section="stoplist", title="Стоп-лист",
                  description="Телефоны, ники, работодатели и ключевые слова, из-за которых вакансия не публикуется.")


@router.post("/admin/stoplist/add")
async def stoplist_add(request: Request, admin: Admin) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        kind, value = form.get("kind"), form.get("value")
        with dbc(request) as conn:
            try:
                entry_id, created = stoplist.add(conn, kind, value, actor=admin, note=form.get("note"),
                                                 now=state_of(request).clock())
            except ValueError as exc:
                ctx = stoplist_page_ctx(conn, request, error=str(exc), form=form.first_values())
                return render(request, "stoplist.html", ctx, 422, section="stoplist", title="Стоп-лист",
                              description="Телефоны, ники, работодатели и ключевые слова, из-за которых вакансия не публикуется.")
            if form.has("apply_existing"):
                stoplist.apply_to_existing(conn, admin, entry_id, state_of(request).clock())
        return see_other("/admin/stoplist", "stop_added" if created else "stop_exists")

    return await run_in_threadpool(work)


@router.post("/admin/stoplist/{entry_id}/delete")
async def stoplist_delete(request: Request, admin: Admin, entry_id: int) -> Response:
    await admin_form(request, admin)

    def work() -> Response:
        with dbc(request) as conn:
            if not stoplist.remove(conn, entry_id, admin, state_of(request).clock()):
                raise HTTPException(404)
        return see_other("/admin/stoplist", "stop_removed")

    return await run_in_threadpool(work)


# ------------------------------------------------------------------ запросы по персональным данным

REQUEST_VIEWS = legal_requests.VIEWS


@router.get("/admin/requests")
def requests_list(request: Request, admin: Admin) -> Response:
    view = view_of(request, REQUEST_VIEWS, "open")
    st = state_of(request)
    with dbc(request) as conn:
        page = legal_requests.queue(conn, view, page_no(request), st.clock())
        counts = legal_requests.counts(conn, st.clock())
    return render(request, "requests.html", requests_ctx(view, page, counts), section="requests", title="Запросы по персональным данным",
                  description="Журнал обращений: от кого, суть, срок ответа, решение и исполнение. Просроченные подсвечены.")


def requests_ctx(view: str, page: Any, counts: dict[str, int], **extra: Any) -> dict[str, Any]:
    return {
        "view": view, "views": REQUEST_VIEWS, "page": page, "counts": counts, "form": {}, "error": "",
        "kinds": [(k, v[0]) for k, v in legal_requests.TYPES.items()], "channels": list(legal_requests.CHANNELS.items()),
        **extra,
    }


@router.get("/admin/requests.csv")
def requests_csv(request: Request, admin: Admin) -> Response:
    with dbc(request) as conn:
        body = legal_requests.export_csv(conn, state_of(request).clock())
        audit.log(conn, admin, "request.export", "request", "all", ip=ip_of(request), now=state_of(request).clock())
    return Response("﻿" + body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="obrasheniya.csv"'})


def request_context(conn: sqlite3.Connection, request: Request, request_id: int, **extra: Any) -> dict[str, Any]:
    r = legal_requests.get(conn, request_id, state_of(request).clock())
    if r is None:
        raise HTTPException(404)
    guess_kind = "phone"
    guess_value = ""
    import re as _re

    for token in _re.findall(r"[+\d][\d\s()\-]{8,}|@[A-Za-z0-9_]{3,}", f"{r['subject']} {r['body']}"):
        guess_kind = "nick" if token.startswith("@") else "phone"
        guess_value = token.strip()
        break
    return {
        "r": r, "statuses": list(legal_requests.STATUSES.items()), "decisions": list(legal_requests.DECISIONS.items()),
        "error": "", "guess_kind": guess_kind, "guess_value": guess_value, "erase_form": {}, **extra,
    }


def request_error(request: Request, request_id: int, message: str, **extra: Any) -> Response:
    with dbc(request) as conn:
        ctx = request_context(conn, request, request_id, error=message, **extra)
    return render(request, "request.html", ctx, 422, section="requests", title=f"Обращение {request_id}",
                  description="Обращение по персональным данным: суть, срок, решение, ответ и удаление данных по контакту.")


@router.get("/admin/requests/{request_id}")
def request_page(request: Request, admin: Admin, request_id: int) -> Response:
    with dbc(request) as conn:
        ctx = request_context(conn, request, request_id)
    return render(request, "request.html", ctx, section="requests", title=f"Обращение {request_id}",
                  description="Обращение по персональным данным: суть, срок, решение, ответ и удаление данных по контакту.")


@router.post("/admin/requests/new")
async def request_new(request: Request, admin: Admin) -> Response:
    form = await admin_form(request, admin)
    st = state_of(request)

    def work() -> Response:
        received = form.get("received_on").strip()
        created = st.clock()
        error = ""
        if received:
            try:
                created = datetime.fromisoformat(received).replace(hour=12, tzinfo=UTC)
            except ValueError:
                error = "Дата получения: формат ГГГГ-ММ-ДД."
        with dbc(request) as conn:
            if not error:
                try:
                    legal_requests.create(
                        conn, form.get("kind"), now=created, name=form.get("name"), contact=form.get("contact"),
                        subject=form.get("subject"), body=form.get("body"), channel=form.get("channel") or "email", actor=admin)
                except ValueError as exc:
                    error = str(exc)
            if error:
                page = legal_requests.queue(conn, "open", 1, st.clock())
                ctx = requests_ctx("open", page, legal_requests.counts(conn, st.clock()), error=error,
                                   form=form.first_values(), open_form=True)
                return render(request, "requests.html", ctx, 422, section="requests", title="Запросы по персональным данным",
                              description="Журнал обращений: от кого, суть, срок ответа, решение и исполнение.")
        return see_other("/admin/requests", "request_created")

    return await run_in_threadpool(work)


@router.post("/admin/requests/{request_id}/update")
async def request_update(request: Request, admin: Admin, request_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        with dbc(request) as conn:
            if legal_requests.get(conn, request_id) is None:
                raise HTTPException(404)
            try:
                legal_requests.update(
                    conn, request_id, admin, state_of(request).clock(), status=form.get("status") or None,
                    decision=form.get("decision"), decision_code=form.get("decision_code"), admin_note=form.get("admin_note"))
            except ValueError as exc:
                return request_error(request, request_id, str(exc))
        return see_other(f"/admin/requests/{request_id}", "request_updated")

    return await run_in_threadpool(work)


@router.post("/admin/requests/{request_id}/erase")
async def request_erase(request: Request, admin: Admin, request_id: int) -> Response:
    form = await admin_form(request, admin)

    def work() -> Response:
        with dbc(request) as conn:
            if legal_requests.get(conn, request_id) is None:
                raise HTTPException(404)
            try:
                legal_requests.erase_contact(conn, form.get("kind"), form.get("value"), admin, state_of(request).clock(),
                                             request_id=request_id)
            except ValueError as exc:
                return request_error(request, request_id, str(exc), erase_form=form.first_values())
        return see_other(f"/admin/requests/{request_id}", "erased")

    return await run_in_threadpool(work)


# ------------------------------------------------------------------ журнал

@router.get("/admin/audit")
def audit_page(request: Request, admin: Admin) -> Response:
    prefix = clean_line(request.query_params.get("action", ""), 40)
    with dbc(request) as conn:
        page = audit.page(conn, page_no(request), prefix)
    return render(request, "audit.html", {"page": page, "action": prefix}, section="audit", title="Журнал действий",
                  description="Кто и что делал в админке: действие, объект, время и адрес. Записи не редактируются.")


# ------------------------------------------------------------------ публичная форма запроса

CAPTCHA_TTL = timedelta(hours=1)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, b"captcha:" + payload.encode(), hashlib.sha256).hexdigest()[:20]


def make_captcha(secret: bytes, now: datetime) -> dict[str, Any]:
    """Вопрос-сумма без хранения на сервере: слагаемые и время подписаны ключом."""
    a, b = 2 + secrets.randbelow(8), 2 + secrets.randbelow(8)
    ts = int(now.timestamp())
    payload = f"{a}.{b}.{ts}"
    return {"a": a, "b": b, "token": f"{payload}.{_sign(secret, payload)}"}


def check_captcha(secret: bytes, token: str, answer: str, now: datetime) -> bool:
    try:
        a, b, ts, sig = token.split(".")
        payload = f"{int(a)}.{int(b)}.{int(ts)}"
        total = int(a) + int(b)
        sent = int(answer.strip())
        issued = datetime.fromtimestamp(int(ts), UTC)
    except (ValueError, OverflowError, OSError):
        return False
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    if not (-timedelta(minutes=2) <= now - issued <= CAPTCHA_TTL):
        return False
    return sent == total


def public_form_ctx(request: Request, form: dict[str, str] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    st = state_of(request)
    cap = make_captcha(secret_of(request), st.clock())
    kinds = [(k, legal_requests.TYPES[k][0]) for k in legal_requests.PUBLIC_KINDS]
    return {
        "form": form or {"kind": "access"}, "errors": errors or {}, "kinds": kinds, "captcha": cap,
        "deadlines": [(legal_requests.TYPES[k][0], legal_requests.TYPES[k][1], legal_requests.TYPES[k][2])
                      for k in legal_requests.PUBLIC_KINDS],
        "sent": None, "contact_email": config.get("CONTACT_EMAIL") or "",
        "seo": {
            "title": "Запрос по персональным данным",
            "description": "Форма обращения: доступ к данным, уточнение, удаление, отзыв согласия и просьба убрать вакансию. "
                           "Срок ответа зависит от типа запроса.",
            "noindex": True,
        },
    }


def site_render(request: Request, ctx: dict[str, Any], status: int = 200) -> Response:
    from rabota.web.auth_routes import render as auth_render

    return auth_render(request, "legal/request.html", ctx, status)


@router.get("/legal/request")
def public_request_form(request: Request) -> Response:
    ctx = public_form_ctx(request)
    sent = request.query_params.get("sent")
    if sent is not None:
        ctx["sent"] = sent if sent.isdigit() else "0"
        ctx["seo"]["title"] = "Обращение принято"
    return site_render(request, ctx)


@router.post("/legal/request")
async def public_request_submit(request: Request) -> Response:
    form = await read_form(request)
    check_csrf(request, form.first_values())
    st = state_of(request)
    if not st.limiter.allow("legal_request", ip_of(request)):
        raise HTTPException(429)
    values = form.first_values()
    if values.get("website"):  # ловушка для ботов: делаем вид, что приняли
        return see_other("/legal/request?sent=0")

    kind = values.get("kind", "")
    name = clean_line(values.get("name"), 120)
    contact = clean_line(values.get("contact"), 200)
    subject = clean_line(values.get("subject"), 300)
    body = " ".join(values.get("body", "").split())[:3000]
    errors: dict[str, str] = {}
    if kind not in legal_requests.PUBLIC_KINDS:
        errors["kind"] = "Выберите тип обращения."
    if not contact:
        errors["contact"] = "Укажите почту или телефон, чтобы мы могли ответить."
    elif not (service.valid_email(contact.lower()) or service.normalize_phone(contact)):
        errors["contact"] = "Нужна почта вида name@example.ru или телефон."
    if len(body) < 10:
        errors["body"] = "Опишите, что нужно сделать, хотя бы в паре слов."
    if not check_captcha(secret_of(request), values.get("captcha_token", ""), values.get("captcha_answer", ""), st.clock()):
        errors["captcha_answer"] = "Неверный ответ на вопрос с суммой. Посчитайте ещё раз."
    if errors:
        shown = {"kind": kind, "name": name, "contact": contact, "subject": subject, "body": body}
        ctx = public_form_ctx(request, shown, errors)
        ctx["seo"]["title"] = "Запрос по персональным данным: проверьте поля"
        return site_render(request, ctx, 422)

    def save() -> int:
        with dbc(request) as conn:
            return legal_requests.create(
                conn, kind, now=st.clock(), name=name, contact=contact, subject=subject, body=body, channel="web",
                requester_key=complaints.reporter_key(secret_of(request), ip_of(request)))

    request_id = await run_in_threadpool(save)
    return see_other(f"/legal/request?sent={request_id}")


# ------------------------------------------------------------------ подключение

def install(
    app: FastAPI, *, db_path: Path | str | None = None, site_url: str = "", clock: Callable[[], datetime] | None = None,
    limiter: RateLimiter | None = None, health: Callable[[sqlite3.Connection], Any] | None = None,
) -> AdminState:
    """Подключить админку: маршруты, шаблоны, заголовки закрытия от индекса. Состояние лежит в app.state.admin."""
    path = Path(db_path) if db_path else config.DB_PATH
    service.ensure_schema(path)
    state = AdminState(
        db_path=path, site_url=site_url.rstrip("/") or "https://работа-донецк.рф", clock=clock or (lambda: datetime.now(UTC)),
        limiter=limiter or RateLimiter(DEFAULT_LIMITS), health=health,
    )
    for bucket, limit in DEFAULT_LIMITS.items():
        state.limiter.limits.setdefault(bucket, limit)
    state.templates = build_templates(state.site_url)
    app.state.admin = state

    @app.middleware("http")
    async def admin_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        p = request.url.path
        if p == "/admin" or p.startswith("/admin/"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers["Cache-Control"] = "no-store"
        elif p == "/legal/request":
            response.headers["X-Robots-Tag"] = "noindex, follow"
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    return state


__all__ = ["Admin", "check_captcha", "install", "iso", "make_captcha", "require_admin"]
