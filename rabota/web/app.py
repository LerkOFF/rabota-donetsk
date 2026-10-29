"""Сайт "Работа Донецк": серверный рендер на FastAPI и Jinja2.

Запуск: .venv/bin/uvicorn rabota.web.app:app --port 8000
База берётся из rabota.db.connect (путь DB_PATH из .env), в тестах и для демо путь передаётся явно.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from rabota import cabinet, config, lifecycle
from rabota import legal as legal_docs
from rabota.accounts.deps import check_csrf
from rabota.accounts.service import valid_email
from rabota.admin import complaints as complaint_service
from rabota.models import CATEGORIES, PAY_PERIODS, WORK_FORMATS
from rabota.ops import deep as deep_health
from rabota.ops.logs import StatusCounterMiddleware
from rabota.web import format as fmt
from rabota.web import queries as q
from rabota.web import seo as seolib
from rabota.web.analytics import install_analytics
from rabota.web.postform import form_options, validate_post
from rabota.web.security import install_security
from rabota.web.seo import job_posting_jsonld, website_jsonld

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

DEFAULT_SITE_URL = "https://работа-донецк.рф"
MAX_BODY = 32 * 1024

COMPLAINT_REASONS = tuple(text for _, text in complaint_service.PUBLIC_REASONS)
COMPLAINT_CODES = {text: code for code, text in complaint_service.PUBLIC_REASONS}

DEFAULT_RATE_LIMITS = {"complaint": (6, 3600), "post": (4, 3600)}

# Пункты меню вошедшего пользователя: ключ, подпись, адрес, значок, показывать в шапке отдельной ссылкой.
# Роли как в таблице users: job_seeker, employer, admin. Адреса кабинетов меняются здесь, в одном месте.
MenuItem = tuple[str, str, str, str, bool]
SEEKER_MENU: tuple[MenuItem, ...] = (
    ("cabinet", "Кабинет", "/cabinet/seeker", "house-line", False),
    ("responses", "Отклики", "/responses", "paper-plane-tilt", True),
    ("messages", "Сообщения", "/messages", "chat-circle-dots", True),
    ("profile", "Профиль", "/account", "user-circle", False),
)
EMPLOYER_MENU: tuple[MenuItem, ...] = (
    ("cabinet", "Кабинет", "/cabinet/employer", "house-line", False),
    ("vacancies", "Мои вакансии", "/cabinet/vacancies", "briefcase", True),
    ("responses", "Отклики", "/responses", "paper-plane-tilt", True),
    ("messages", "Сообщения", "/messages", "chat-circle-dots", True),
    ("company", "Компания", "/cabinet/company", "buildings", False),
    ("profile", "Профиль", "/account", "user-circle", False),
)
ADMIN_MENU: tuple[MenuItem, ...] = (
    ("cabinet", "Кабинет", "/cabinet", "gear-six", False),
    ("profile", "Профиль", "/account", "user-circle", False),
)
ROLE_MENUS = {"job_seeker": SEEKER_MENU, "employer": EMPLOYER_MENU, "admin": ADMIN_MENU}


def current_user(request: Request) -> dict[str, Any] | None:
    """Пользователь из request.state.user (его ставит middleware аккаунтов). Нет атрибута или пусто: гость."""
    user = getattr(request.state, "user", None)
    if not user:
        return None
    data = dict(user) if isinstance(user, Mapping) else {k: getattr(user, k, None) for k in ("id", "email", "name", "role")}
    role = data.get("role") or "job_seeker"
    data["role"] = role
    label = (data.get("name") or data.get("email") or "").strip() or "Профиль"
    data["label"] = label
    data["initial"] = label[:1].upper()
    data["menu"] = [
        {"key": k, "label": t, "href": h, "icon": i, "quick": q} for k, t, h, i, q in ROLE_MENUS.get(role, SEEKER_MENU)
    ]
    data["is_employer"] = role == "employer"
    return data


class RateLimiter:
    """Лимит частоты в памяти процесса: не больше N отправок за окно секунд с одного адреса."""

    def __init__(self, limits: dict[str, tuple[int, int]], clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, bucket: str, client: str) -> bool:
        limit, window = self.limits.get(bucket, (0, 0))
        if limit <= 0:
            return True
        now = self.clock()
        hits = self._hits[(bucket, client)]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def create_app(
    db_path: Path | str | None = None,
    *,
    site_url: str | None = None,
    premoderate: bool = True,
    rate_limits: dict[str, tuple[int, int]] | None = None,
    now: Callable[[], datetime] | None = None,
    auth_options: Mapping[str, Any] | None = None,
) -> FastAPI:
    """Собирает приложение. premoderate=True: вакансии из формы ждут проверки и в ленте не видны.
    auth_options: необязательные параметры подключения аккаунтов (почтовик, часы, лимиты, секрет), нужны тестам."""
    app = FastAPI(title="Работа Донецк", docs_url=None, redoc_url=None, openapi_url=None)
    base_url = (site_url or config.get("SITE_URL") or DEFAULT_SITE_URL).rstrip("/")
    limiter = RateLimiter({**DEFAULT_RATE_LIMITS, **(rate_limits or {})})
    clock = now or (lambda: datetime.now(UTC))
    trust_proxy = config.get("WEB_TRUST_PROXY") == "1"

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.globals.update(
        asset=lambda name: f"/static/{name}?v={_asset_version(name)}",
        pay_parts=fmt.pay_parts,
        category_icon=lambda c: fmt.CATEGORY_ICONS.get(c, "dots-three-circle"),
        category_label=lambda c: fmt.CATEGORY_LABELS.get(c, str(c).capitalize()),
        work_format_label=lambda c: fmt.WORK_FORMAT_LABELS.get(c, c),
        count_label=fmt.count_label,
        plural=fmt.plural,
        money=fmt.money,
        district_label=fmt.district_label,
        phone_pretty=fmt.phone_pretty,
        phone_href=fmt.phone_href,
        telegram_href=fmt.telegram_href,
        site_url=base_url,
    )
    env.filters["ucfirst"] = lambda s: (s[:1].upper() + s[1:]) if s else s
    env.filters["quote"] = lambda s: quote(str(s), safe="")

    def db() -> Any:
        return q.open_conn(db_path)

    hints_cache: dict[str, Any] = {"at": -1e9, "data": {"professions": [], "cities": [], "districts": []}}

    def search_hints() -> dict[str, list[str]]:
        """Подсказки строки поиска, обновляются раз в полминуты: страницы не ходят за ними в базу каждый раз."""
        if time.monotonic() - hints_cache["at"] > 30:
            try:
                with db() as conn:
                    hints_cache["data"] = q.search_hints(conn)
            except sqlite3.Error:
                pass
            hints_cache["at"] = time.monotonic()
        return hints_cache["data"]

    env.globals["search_hints"] = search_hints

    # журнал WAL: сборщики пишут, сайт читает без блокировок; если базу открыть нельзя, сайт всё равно стартует
    with contextlib.suppress(sqlite3.Error), db() as c:
        c.execute("PRAGMA journal_mode=WAL")

    def render(request: Request, name: str, context: dict[str, Any] | None = None, status: int = 200) -> Response:
        ctx: dict[str, Any] = {
            "path": request.url.path, "seo": {}, "user": current_user(request),
            "csrf_token": getattr(request.state, "csrf_token", "") or "",
            "contact_email": config.get("CONTACT_EMAIL") or "",
        }
        ctx.update(context or {})
        ctx["seo"] = seolib.finalize(ctx["seo"], base_url, request.url.path)
        response = templates.TemplateResponse(request, name, ctx, status_code=status)
        seolib.apply_headers(response, ctx["seo"])
        return response

    def present(v: dict[str, Any]) -> dict[str, Any]:
        return present_vacancy(v, clock())

    def client_ip(request: Request) -> str:
        if trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin or origin == "null":
            return origin is None
        return urlparse(origin).netloc == request.headers.get("host", "")

    async def read_form(request: Request) -> dict[str, str] | None:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return None
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=60)
        return {k: v[0] for k, v in parsed.items()}

    # ---------------------------------------------------------------- служебные

    # заголовки безопасности, кеш статики, счётчики ответов, счётчик Метрики и файл Вебмастера: rabota/web/security.py и др.
    analytics = install_analytics(app)
    install_security(app, site_url=base_url, metrika=analytics.enabled)
    app.add_middleware(StatusCounterMiddleware)

    seolib.install_headers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return render(request, "error.html", {"code": 404, "seo": {
                "title": "Страница не найдена", "noindex": True,
                "description": "Такой страницы нет: вакансию могли убрать или ссылка набрана с ошибкой. "
                               "Посмотрите свежие вакансии Донецка и ДНР."}}, 404)
        if exc.status_code == 429:
            return render(request, "error.html", {"code": 429, "seo": {
                "title": "Слишком часто", "noindex": True,
                "description": "Вы отправили много форм подряд. Подождите немного и попробуйте снова: "
                               "ограничение нужно, чтобы защитить сайт от спама."}}, 429)
        return render(
            request, "error.html",
            {"code": exc.status_code, "detail": exc.detail, "seo": {
                "title": "Запрос не выполнен", "noindex": True,
                "description": "Запрос не удалось выполнить. Вернитесь на главную страницу или откройте ленту "
                               "свежих вакансий Донецка и ДНР."}},
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        return render(request, "error.html", {"code": 500, "seo": {
            "title": "Ошибка на сайте", "noindex": True,
            "description": "На сайте произошла ошибка, мы уже знаем о ней и чиним. Обновите страницу через минуту "
                           "или вернитесь на главную."}}, 500)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    deep_health.install(app, db_path)

    # ---------------------------------------------------------------- страницы

    @app.get("/")
    def index(request: Request) -> Response:
        with db() as conn:
            stats = q.stats(conn, clock())
            facets = q.facets(conn)
            fresh = [present(v) for v in q.latest(conn, 8)]
            district_city = q.district_city_map(conn)
        return render(request, "index.html", {
            "stats": stats,
            "categories": facets["categories"],
            "professions": facets["professions"][:14],
            "districts": [(d, n, district_city.get(d, "")) for d, n in facets["districts"][:10]],
            "cities": facets["cities"][:6],
            "fresh": fresh,
            "seo": {
                "title": "Работа в Донецке и ДНР: свежие вакансии",
                "description": DEFAULT_DESCRIPTION,
                "jsonld": [website_jsonld(base_url), seolib.organization_jsonld(base_url, config.get("CONTACT_EMAIL") or "")],
            },
        })

    @app.get("/vacancies")
    def vacancies(request: Request) -> Response:
        page_no = q.parse_page(request.query_params.get("page"))
        with db() as conn:
            facets = q.facets(conn)
            flt = q.Filters.from_params(resolve_where(request.query_params, facets))
            page = q.search(conn, flt, page_no)
        items = [present(v) for v in page.items]
        title = "Вакансии в Донецке и ДНР"
        if flt.q:
            title = f"{flt.q.capitalize()}: вакансии в Донецке"
        elif flt.category:
            title = f"Вакансии: {fmt.CATEGORY_LABELS.get(flt.category, flt.category).lower()}"
        on_page = f" Страница {page.page} из {page.pages}." if page.page > 1 else ""
        # индексируется только чистая лента с вакансиями: фильтры, поиск и сортировка закрыты, каноника ведёт на ленту
        noindex = not flt.is_empty or page.total < seolib.MIN_INDEXABLE
        return render(request, "vacancies.html", {
            "flt": flt, "page": page, "items": items, "facets": facets,
            "where": flt.district and fmt.district_label(flt.district) or flt.city,
            "sorts": SORT_LABELS,
            "chips": active_chips(flt),
            "options": filter_options(facets, flt),
            "base_qs": flt.query_string(),
            "seo": {
                "title": title + (f", страница {page.page}" if page.page > 1 else ""),
                "description": f"Свежие вакансии Донецка и ДНР: {fmt.count_label(page.total, 'вакансия', 'вакансии', 'вакансий')} "
                               f"с оплатой, графиком, районом и телефоном. Ссылка на оригинал, фильтры по профессии, "
                               f"подработке и ежедневной выплате.{on_page}",
                "noindex": noindex,
                "canonical": base_url + "/vacancies" + (f"?page={page.page}" if page.page > 1 and flt.is_empty else ""),
                "prev_url": _page_url("/vacancies", flt.query_string(), page.page - 1) if page.has_prev else "",
                "next_url": _page_url("/vacancies", flt.query_string(), page.page + 1) if page.has_next else "",
                "breadcrumbs": [("Главная", "/"), ("Вакансии", "")],
                "jsonld": seolib.item_list_jsonld(base_url, items, (page.page - 1) * page.per_page),
            },
        })

    def listing(request: Request, kind: str, raw_name: str) -> Response:
        """Страница-посадка: профессия, район или город. Вводный текст и ссылки собираются из данных выдачи."""
        raw_name = raw_name.strip()[:80]
        page_no = q.parse_page(request.query_params.get("page"))
        with db() as conn:
            name = q.canonical_name(conn, kind, raw_name) or raw_name
            name = name.lower() if kind == "profession" else name
            flt = q.Filters(**{kind: name})
            page = q.search(conn, flt, page_no)
            if page.total == 0:
                raise StarletteHTTPException(404)
            facts = q.listing_facts(conn, flt)
            indexable = q.indexable_counts(conn, seolib.MIN_INDEXABLE)
            category = facts["categories"][0][0] if facts["categories"] else ""
            related = q.related_professions(conn, category, name, seolib.MIN_INDEXABLE) if kind == "profession" else []
        items = [present(v) for v in page.items]
        landing = landing_page(kind, name, page.total, facts)
        path = f"/{kind}/{name}"
        on_page = f", страница {page.page}" if page.page > 1 else ""
        count = fmt.count_label(page.total, "вакансия", "вакансии", "вакансий")
        title = landing["title"].replace("{count}", count) + on_page
        description = (f"Страница {page.page} из {page.pages}. " if page.page > 1 else "") + seolib.listing_description(
            landing["short"], page.total, facts, places=landing["places"])
        return render(request, "listing.html", {
            "heading": landing["heading"], "kind": kind, "name": name, "page": page, "items": items,
            "intro": seolib.listing_intro(landing["lead"].replace("{count}", count_active(page.total)), facts,
                                          places=landing["places"]),
            "groups": landing_links(kind, name, facts, indexable, related),
            "list_path": _quote_path(path),
            "seo": {
                "title": title,
                "description": description,
                "noindex": page.total < seolib.MIN_INDEXABLE,
                "canonical": seolib.path_url(base_url, path, f"page={page.page}" if page.page > 1 else ""),
                "prev_url": _page_url(_quote_path(path), "", page.page - 1) if page.has_prev else "",
                "next_url": _page_url(_quote_path(path), "", page.page + 1) if page.has_next else "",
                "breadcrumbs": [("Главная", "/"), ("Вакансии", "/vacancies"), (landing["crumb"], path)],
                "jsonld": seolib.item_list_jsonld(base_url, items, (page.page - 1) * page.per_page),
            },
        })

    @app.get("/profession/{name}")
    def profession_page(request: Request, name: str) -> Response:
        return listing(request, "profession", name)

    @app.get("/district/{name}")
    def district_page(request: Request, name: str) -> Response:
        return listing(request, "district", name)

    @app.get("/city/{name}")
    def city_page(request: Request, name: str) -> Response:
        return listing(request, "city", name)

    def vacancy_page_context(conn: Any, vacancy_id: int) -> dict[str, Any]:
        raw = q.get_vacancy(conn, vacancy_id)
        if raw is None or raw["kind"] != "vacancy" or raw["status"] not in ("active", "closed", "expired"):
            raise StarletteHTTPException(404)
        if raw["duplicate_of"]:
            main = q.get_vacancy(conn, raw["duplicate_of"])
            if main and q.is_listable(main):
                raise _Redirect(f"/vacancy/{main['id']}")
            raise StarletteHTTPException(404)
        # закрыта работодателем, скрыта по сроку жизни или не публиковалась дольше срока: страницы больше нет (410)
        closed = raw["status"] != "active" or lifecycle.is_stale(raw.get("last_seen_at") or raw.get("published_at"), now=clock())
        v = present(raw)
        similar = [present(s) for s in q.similar(conn, raw)]
        also = present_also(q.also_published_in(conn, raw["id"]))
        # на проверке или помечена подозрительной: страница открывается по ссылке, но в индекс не идёт
        hidden = bool(raw.get("suspicious")) or (raw.get("moderation_status") or "approved") != "approved"
        return {"v": v, "closed": closed, "hidden": hidden, "similar": similar, "also": also}

    class _Redirect(Exception):
        def __init__(self, url: str):
            self.url = url

    @app.exception_handler(_Redirect)
    async def redirect_handler(request: Request, exc: _Redirect) -> Response:
        return RedirectResponse(exc.url, status_code=301)

    def vacancy_seo(ctx: dict[str, Any]) -> dict[str, Any]:
        v = ctx["v"]
        where = v["location"] or "Донецк"
        pay_text = v["pay"]["text"]
        # самый подробный заголовок, который помещается в 60 знаков; иначе без района, иначе только профессия
        heads = [f"{v['profession_title']}, {where}: {pay_text}",
                 f"{v['profession_title']}, {v['city'] or 'Донецк'}: {pay_text}",
                 f"{v['profession_title']}: {pay_text}"]
        title = next((h for h in heads if len(h) <= seolib.TITLE_LIMIT), heads[-1])
        desc = f"{v['profession_title']}, {where}. {pay_text if not v['pay']['known'] else 'Оплата: ' + pay_text}."
        if v["schedule"]:
            desc += f" График {v['schedule']}."
        if v["summary"]:
            desc += " " + v["summary"]
        crumbs = [("Главная", "/"), ("Вакансии", "/vacancies")]
        if v["city"]:
            crumbs.append((v["city"], f"/city/{v['city']}"))
        crumbs.append((v["profession_title"], f"/vacancy/{v['id']}"))
        noindex = ctx["closed"] or ctx.get("hidden", False)
        return {
            "title": title, "description": desc, "og_type": "article", "noindex": noindex,
            "breadcrumbs": crumbs,
            "jsonld": None if noindex else job_posting_jsonld(v, base_url),
        }

    @app.get("/vacancy/{vacancy_id}")
    def vacancy_view(request: Request, vacancy_id: int) -> Response:
        with db() as conn:
            ctx = vacancy_page_context(conn, vacancy_id)
        ctx["complained"] = request.query_params.get("complaint") == "sent"
        ctx["reasons"] = COMPLAINT_REASONS
        ctx["form"] = {}
        ctx["errors"] = {}
        ctx["seo"] = vacancy_seo(ctx)
        return render(request, "vacancy.html", ctx, 410 if ctx["closed"] else 200)

    @app.post("/vacancy/{vacancy_id}/complaint")
    async def complaint(request: Request, vacancy_id: int) -> Response:
        if not same_origin(request):
            raise StarletteHTTPException(403, "Запрос с другого сайта")
        form = await read_form(request)
        if form is None:
            raise StarletteHTTPException(413, "Слишком большой запрос")
        if not limiter.allow("complaint", client_ip(request)):
            raise StarletteHTTPException(429)

        def work() -> Response | dict[str, Any]:
            with db() as conn:
                ctx = vacancy_page_context(conn, vacancy_id)
                if form.get("website"):  # ловушка для ботов: делаем вид, что приняли
                    return RedirectResponse(f"/vacancy/{vacancy_id}?complaint=sent", status_code=303)
                reason = form.get("reason", "").strip()
                comment = " ".join(form.get("comment", "").split())[:500]
                errors: dict[str, str] = {}
                if reason not in COMPLAINT_REASONS:
                    errors["reason"] = "Выберите причину из списка."
                if reason == "Другое" and len(comment) < 5:
                    errors["comment"] = "Опишите, что не так, хотя бы в паре слов."
                email = form.get("email", "").strip().lower()[:254]
                if email and not valid_email(email):
                    errors["email"] = "Проверьте адрес почты или оставьте поле пустым."
                if errors:
                    ctx.update({"errors": errors, "form": {"reason": reason, "comment": comment, "email": email}})
                    return ctx
                text = reason + (f": {comment}" if comment else "")
                secret = getattr(getattr(request.app.state, "accounts", None), "secret", b"")
                q.add_complaint(
                    conn, vacancy_id, text, code=COMPLAINT_CODES[reason], reporter_email=email or None,
                    reporter_key=complaint_service.reporter_key(secret, client_ip(request)),
                )
                complaint_service.autohide_if_needed(conn, vacancy_id, now=clock())
                return RedirectResponse(f"/vacancy/{vacancy_id}?complaint=sent", status_code=303)

        try:
            result = await run_in_threadpool(work)
        except _Redirect as r:
            return RedirectResponse(r.url, status_code=301)
        if isinstance(result, Response):
            return result
        result.update({"complained": False, "reasons": COMPLAINT_REASONS, "complaint_open": True})
        result["seo"] = {**vacancy_seo(result), "noindex": True}
        return render(request, "vacancy.html", result, 422)

    # ---------------------------------------------------------------- размещение

    def post_context(form: dict[str, str] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "form": form or {"city": "Донецк"},
            "errors": errors or {},
            **form_options(),
            "form_action": "/razmestit",
            "seo": {
                "title": "Разместить вакансию в Донецке бесплатно",
                "description": "Опубликуйте вакансию в Донецке и ДНР: профессия, оплата, график, телефон. "
                               "Бесплатно, после короткой проверки, соискатели увидят её в ленте.",
            },
        }

    def poster(request: Request) -> dict[str, Any] | None:
        """Вошедший работодатель: его вакансии привязываются к аккаунту и компании. Гость и другие роли: None."""
        user = getattr(request.state, "user", None)
        if isinstance(user, Mapping) and user.get("role") == "employer":
            return dict(user)
        return None

    def employer_defaults(request: Request, user: dict[str, Any]) -> dict[str, str]:
        """Начальные значения формы работодателя: название, телефон и почта из карточки компании."""
        conn = request.app.state.accounts.db()
        try:
            company = cabinet.get_company(conn, int(user["id"])) or {}
        finally:
            conn.close()
        return {
            "employer": company.get("name") or "",
            "phone": company.get("phone") or user.get("phone") or "",
            "email": company.get("contact_email") or "",
        }

    @app.get("/razmestit")
    async def post_form(request: Request) -> Response:
        ctx = post_context()
        user = poster(request)
        if user:
            defaults = await run_in_threadpool(employer_defaults, request, user)
            ctx["form"] = {"city": "Донецк", **{k: v for k, v in defaults.items() if v}}
        return render(request, "post.html", ctx)

    @app.post("/razmestit")
    async def post_submit(request: Request) -> Response:
        if not same_origin(request):
            raise StarletteHTTPException(403, "Запрос с другого сайта")
        form = await read_form(request)
        if form is None:
            raise StarletteHTTPException(413, "Слишком большой запрос")
        if not limiter.allow("post", client_ip(request)):
            raise StarletteHTTPException(429)
        user = poster(request)
        if user:  # вошедший работодатель: форма защищена токеном, как остальные формы кабинета
            check_csrf(request, form)
        elif form.get("website"):
            return RedirectResponse("/razmestit/spasibo", status_code=303)
        vacancy, cleaned, errors = validate_post(form)
        if errors or vacancy is None:
            ctx = post_context(cleaned, errors)
            ctx["seo"]["noindex"] = True
            return render(request, "post.html", ctx, 422)

        def save() -> int:
            with db() as conn:
                if user is None:
                    return q.add_site_vacancy(conn, vacancy, premoderate=premoderate)
                company_id = cabinet.ensure_company(conn, user, vacancy.employer or "", vacancy.city or "")
                company = cabinet.get_company(conn, int(user["id"])) or {}
                vacancy.employer = vacancy.employer or company.get("name") or None
                return q.add_site_vacancy(
                    conn, vacancy, premoderate=premoderate, owner_user_id=int(user["id"]), company_id=company_id)

        vacancy_id = await run_in_threadpool(save)
        if user:
            return RedirectResponse("/cabinet/vacancies?done=" + ("posted" if premoderate else "posted_live"), status_code=303)
        target = "/razmestit/spasibo" if premoderate else f"/vacancy/{vacancy_id}"
        return RedirectResponse(target, status_code=303)

    @app.get("/razmestit/spasibo")
    def post_thanks(request: Request) -> Response:
        return render(request, "post_thanks.html", {"seo": {
            "title": "Вакансия отправлена", "noindex": True,
            "description": "Вакансия отправлена на проверку и появится в ленте, как только мы убедимся, "
                           "что это настоящее предложение о работе."}})

    @app.get("/employers")
    def employers(request: Request) -> Response:
        with db() as conn:
            stats = q.stats(conn, clock())
        return render(request, "employers.html", {
            "stats": stats,
            "faq": EMPLOYER_FAQ,
            "seo": {
                "title": "Найти сотрудников в Донецке бесплатно",
                "description": "Опубликуйте вакансию, получайте отклики соискателей и общайтесь с ними на сайте. "
                               "Бесплатно, после короткой проверки.",
                "jsonld": seolib.faq_jsonld(EMPLOYER_FAQ),
            },
        })

    @app.get("/company/{company_id}")
    def company_page(request: Request, company_id: int) -> Response:
        with db() as conn:
            row = q.get_company(conn, company_id)
            vacancies_ = [present(v) for v in q.company_vacancies(conn, company_id)]
        company = company_stub(company_id, row)
        # в индексе только настоящий профиль, у которого есть открытые вакансии
        noindex = row is None or not vacancies_
        about = f" {seolib.sentence(seolib.truncate(company['description'], 90))}" if company["description"] else ""
        return render(request, "company.html", {
            "company": company, "items": vacancies_,
            "seo": {
                "title": f"{company['name']}: вакансии и о компании",
                "description": f"Работодатель {company['name']} на сайте Работа Донецк: "
                               f"{fmt.count_label(len(vacancies_), 'открытая вакансия', 'открытые вакансии', 'открытых вакансий')}."
                               f"{about}",
                "noindex": noindex,
                "breadcrumbs": [("Главная", "/"), ("Вакансии", "/vacancies"), (company["name"], f"/company/{company_id}")],
                "jsonld": seolib.company_jsonld(base_url, company),
            },
        })

    @app.get("/about")
    def about(request: Request) -> Response:
        return render(request, "about.html", {"seo": {
            "title": "О сайте",
            "description": "Как мы собираем вакансии Донецка и ДНР из каналов и сайтов, чем отличаемся от них "
                           "и как не попасть на обман при поиске работы.",
            "breadcrumbs": [("Главная", "/"), ("О сайте", "/about")],
        }})

    # ---------------------------------------------------------------- для поисковиков

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(seolib.robots_txt(base_url))

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=301)

    def sitemap_groups() -> dict[str, list[tuple[str, str]]]:
        """Адреса карты сайта по видам: (путь, lastmod). Только страницы, которые открыты для индекса."""
        now_ = clock()
        with db() as conn:
            entries = q.sitemap_entries(conn, seolib.MIN_INDEXABLE)
        latest = seolib.date_of(entries["latest"], now_)

        def file_date(name: str) -> str:
            try:
                return datetime.fromtimestamp((TEMPLATES_DIR / name).stat().st_mtime, UTC).date().isoformat()
            except OSError:
                return now_.date().isoformat()

        def doc_date(filename: str) -> str:
            try:
                return datetime.fromtimestamp((legal_docs.LEGAL_DIR / filename).stat().st_mtime, UTC).date().isoformat()
            except OSError:
                return now_.date().isoformat()

        static = [
            ("/", latest), ("/vacancies", latest), ("/employers", file_date("employers.html")),
            ("/razmestit", file_date("post.html")), ("/about", file_date("about.html")),
            ("/legal", max(doc_date(d.filename) for d in legal_docs.list_documents())),
        ]
        static += [(f"/legal/{d.slug}", doc_date(d.filename)) for d in legal_docs.list_documents()]
        return {
            "static": static,
            "professions": [(f"/profession/{p.lower()}", seolib.date_of(ts, now_)) for p, ts in entries["professions"]],
            "districts": [(f"/district/{d}", seolib.date_of(ts, now_)) for d, ts in entries["districts"]]
            + [(f"/city/{c}", seolib.date_of(ts, now_)) for c, ts in entries["cities"]],
            "companies": [(f"/company/{cid}", seolib.date_of(ts, now_)) for cid, ts in entries["companies"]],
            "vacancies": [(f"/vacancy/{vid}", seolib.date_of(ts, now_)) for vid, ts in entries["vacancies"]],
        }

    def xml_response(body: str) -> Response:
        return Response(body, media_type="application/xml")

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap() -> Response:
        """Один файл, пока адресов не больше 5000, дальше индекс с файлами по видам страниц."""
        groups = sitemap_groups()
        if sum(len(v) for v in groups.values()) <= seolib.SITEMAP_CHUNK:
            return xml_response(seolib.sitemap_urlset(base_url, [u for kind in groups.values() for u in kind]))
        files = []
        for kind, urls in groups.items():
            for i, part in enumerate(seolib.chunks(urls), 1):
                if part:
                    files.append((f"/sitemap-{kind}-{i}.xml", max(d for _, d in part)))
        return xml_response(seolib.sitemap_index(base_url, files))

    @app.get("/sitemap-{kind}-{number:int}.xml", include_in_schema=False)
    def sitemap_part(kind: str, number: int) -> Response:
        parts = seolib.chunks(sitemap_groups().get(kind) or [])
        if kind not in ("static", "professions", "districts", "companies", "vacancies") or not 1 <= number <= len(parts):
            raise StarletteHTTPException(404)
        return xml_response(seolib.sitemap_urlset(base_url, parts[number - 1]))

    from rabota.web import admin_routes, legal_routes
    from rabota.web.auth_routes import install as install_auth

    # админка раньше юридических документов: /legal/request не должен попасть под /legal/{slug}
    admin_routes.install(app, db_path=db_path, site_url=base_url, clock=clock)
    legal_routes.install(app, site_url=base_url)
    install_auth(app, db_path=db_path, site_url=base_url, **(auth_options or {}))
    from rabota.web import messages_routes

    messages_routes.install(app, templates=templates)  # отклики, переписка, уведомления
    from rabota.web import cabinet_routes

    cabinet_routes.install(app, premoderate=premoderate)  # вакансии и компания работодателя

    return app


# -------------------------------------------------------------------- помощники

# Вопросы и ответы страницы для работодателей: показываются на странице и уходят в разметку FAQPage
EMPLOYER_FAQ: tuple[tuple[str, str], ...] = (
    ("Сколько стоит размещение", "Сейчас размещение бесплатное. Если условия изменятся, мы предупредим заранее."),
    ("Когда вакансия появится на сайте",
     "После короткой проверки, обычно в течение нескольких часов. Проверяем, что это настоящая вакансия и нет просьбы платить за работу."),
    ("Как соискатели свяжутся со мной",
     "Откликом на сайте или по телефону и в Telegram, если вы их указали. Контакты можно не публиковать и принимать отклики только в кабинете."),
    ("Что делать, если вакансия закрыта",
     "Закройте её в кабинете или нажмите «Пожаловаться» на странице вакансии и выберите «Вакансия не актуальна»."),
)

DEFAULT_DESCRIPTION = (
    "Вакансии Донецка и ДНР в одном месте: бариста, кассир, водитель, грузчик, подработка на смену. "
    "Оплата, график, район и телефон, ссылка на оригинал."
)


def _asset_version(name: str) -> str:
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


def _quote_path(path: str) -> str:
    return quote(path, safe="/%")


def _page_url(base: str, query: str, page: int) -> str:
    parts = [p for p in (query, f"page={page}" if page > 1 else "") if p]
    return base + ("?" + "&".join(parts) if parts else "")


def count_active(n: int) -> str:
    return fmt.count_label(n, "актуальная вакансия", "актуальные вакансии", "актуальных вакансий")


def landing_page(kind: str, name: str, total: int, facts: dict[str, Any]) -> dict[str, Any]:
    """Тексты страницы-посадки по виду: заголовок, начало вводного текста, ключ для title и description."""
    if kind == "profession":
        label = name.capitalize()
        return {
            "heading": f"{label}: вакансии в Донецке", "crumb": label, "short": label,
            "title": label + ": {count} в Донецке", "places": True,
            "lead": f"Профессия «{name}»: сейчас на сайте " + "{count}.",
        }
    if kind == "district":
        label = fmt.district_label(name)
        return {
            "heading": f"Работа: {label}", "crumb": label, "short": label,
            "title": f"Работа: {label}, " + "{count}", "places": False,
            "lead": f"{label}: сейчас на сайте " + "{count}.",
        }
    return {
        "heading": f"Работа в городе {name}", "crumb": name, "short": f"Город {name}",
        "title": f"Работа в городе {name}: " + "{count}", "places": True,
        "lead": f"Город {name}: сейчас на сайте " + "{count}.",
    }


def landing_links(
    kind: str, name: str, facts: dict[str, Any], indexable: dict[str, list[tuple[str, int]]],
    related: list[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Перелинковка страницы-посадки. Ссылки только на страницы, которые сами проиндексируются (3 вакансии и больше),
    и на ленту с условиями, у которых есть вакансии."""
    pro = dict(indexable["professions"])
    dist = dict(indexable["districts"])
    city = dict(indexable["cities"])
    groups: list[dict[str, Any]] = []

    def link_items(pairs: list[tuple[str, int]], allowed: dict[str, int], base: str, label: Callable[[str], str]) -> list[dict[str, Any]]:
        out = []
        for value, n in pairs:
            if value in allowed or value.lower() in allowed:
                slug = value.lower() if base == "profession" else value
                out.append({"label": label(value), "href": f"/{base}/{quote(slug, safe='')}", "n": n, "icon": base == "district"})
        return out

    def add(title: str, items: list[dict[str, Any]]) -> None:
        if items:
            groups.append({"title": title, "items": items})

    def cap(v: str) -> str:
        return v[:1].upper() + v[1:]

    if kind == "profession":
        add(f"Где ищут: {name}", link_items(facts["districts"], dist, "district", fmt.district_label))
        add("Другие профессии в этой сфере", link_items(related[:10], pro, "profession", cap))
        rest = [(p, n) for p, n in indexable["professions"] if p != name and p not in {r for r, _ in related}]
        add("Популярные профессии", link_items(rest[:10], pro, "profession", cap))
    elif kind == "district":
        add("Профессии в этом районе", link_items(facts["professions"], pro, "profession", cap))
        add("Другие районы", link_items([(d, n) for d, n in indexable["districts"] if d != name][:10], dist, "district", fmt.district_label))
    else:
        add("Профессии в этом городе", link_items(facts["professions"], pro, "profession", cap))
        add("Районы", link_items(facts["districts"], dist, "district", fmt.district_label))
        add("Другие города", link_items([(c, n) for c, n in indexable["cities"] if c != name][:8], city, "city", lambda v: v))
    own = {"profession": "q", "district": "district", "city": "city"}[kind]
    queries = []
    for label, flag, key in (("Подработка", "side_job", "side_job"), ("Без опыта", "no_experience", "no_experience"),
                             ("Выплата каждый день", "daily", "daily")):
        if facts.get(key):
            queries.append({"label": label, "href": "/vacancies?" + urlencode({own: name, flag: 1}), "n": facts[key], "icon": False})
    add("Популярные запросы", queries)
    return groups


SORT_LABELS = (("", "По дате"), ("pay", "По оплате"))


def resolve_where(params: Mapping[str, str], facets: dict[str, list[tuple[str, int]]]) -> dict[str, str]:
    """Поле "Где" из строки поиска превращает в город или район. Явно выбранные city и district главнее."""
    out = dict(params)
    where = " ".join((params.get("where") or "").split())[:60]
    if not where or out.get("city") or out.get("district"):
        return out
    key = q.fold(where)
    key_short = key.removesuffix(" район").strip()
    for name, _ in facets["districts"]:
        if q.fold(name) in (key, key_short) or q.fold(fmt.district_label(name)) == key:
            out["district"] = name
            return out
    for name, _ in facets["cities"]:
        if q.fold(name) == key:
            out["city"] = name
            return out
    out["city"] = where
    return out


def company_stub(company_id: int, row: dict[str, Any] | None) -> dict[str, Any]:
    """Данные страницы работодателя: из таблицы companies, а чего нет, заменяют заглушки."""
    row = row or {}
    name = (row.get("name") or "").strip() or "Название компании"
    return {
        "id": company_id,
        "name": name,
        "initial": name[:1].upper(),
        "city": row.get("city") or "Донецк",
        "site": row.get("site") or "",
        "verified": bool(row.get("verified")),
        "description": (row.get("description") or "").strip(),
        "is_stub": not row,
    }


def present_vacancy(v: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Готовит запись для шаблона: подписи, значки, контакты."""
    out = dict(v)
    out["profession_title"] = (v["profession"] or "").strip().capitalize()
    out["pay"] = fmt.pay_parts(v["pay_min"], v["pay_max"], v["pay_period"])
    out["location"] = fmt.location_label(v["city"], v["district"])
    out["work_format_label"] = fmt.WORK_FORMAT_LABELS.get(v["work_format"] or "", "")
    out["employment_label"] = fmt.EMPLOYMENT_LABELS.get(v["employment"] or "", "")
    out["ago"] = fmt.time_ago(v["published_at"] or v.get("created_at"), now)
    out["source_label"] = fmt.SOURCE_LABELS.get(v["source_type"], v["source_type"] or "Источник")
    out["is_site"] = v["source_type"] == "site"
    out["is_direct"] = v.get("origin") == "direct"
    out["company_id"] = v.get("company_id")
    out["apply_url"] = f"/vacancy/{v['id']}/apply" if out["is_direct"] else ""
    exp = (v["experience"] or "").lower()
    out["no_experience"] = exp.startswith(("не нужен", "без опыт"))
    badges: list[dict[str, str]] = []
    if v["pay_daily"]:
        badges.append({"label": "Выплата каждый день", "kind": "pay"})
    if out["no_experience"]:
        badges.append({"label": "Без опыта", "kind": "ok"})
    if v["employment"] in ("подработка", "на сегодня"):
        badges.append({"label": out["employment_label"], "kind": "ok"})
    if v["age_min"] and v["age_min"] > 18:
        badges.append({"label": f"От {v['age_min']} лет", "kind": "plain"})
    out["badges"] = badges
    out["dup_count"] = int(v.get("dup_count") or 0)
    out["also_label"] = also_label(out["dup_count"])
    contacts = v["contacts"] or {}
    phones = [p for p in (contacts.get("phones") or []) if isinstance(p, str) and p.strip()]
    out["phones"] = phones
    out["phone"] = phones[0] if phones else ""
    out["telegram"] = contacts.get("telegram") or ""
    out["max_contact"] = contacts.get("max") or ""
    out["email"] = contacts.get("email") or ""
    out["has_contact"] = bool(phones or out["telegram"] or out["max_contact"] or out["email"])
    out["original_url"] = v["source_url"] if (v["source_url"] or "").startswith(("http://", "https://")) else ""
    return out


def also_label(n: int) -> str:
    """Метка на карточке: "ещё в 2 источниках"."""
    if n <= 0:
        return ""
    return f"ещё в{fmt.NBSP}{n}{fmt.NBSP}{fmt.plural(n, 'источнике', 'источниках', 'источниках')}"


def present_also(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Список "также публикуют" для страницы: подпись типа, название канала, безопасная ссылка."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        stype = item.get("source_type") or ""
        source = item.get("source") or ""
        url = item.get("source_url") or ""
        key = (stype, url or source)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "type": stype,
            "label": fmt.SOURCE_LABELS.get(stype, stype or "Источник"),
            "source": source,
            "url": url if url.startswith(("http://", "https://")) else "",
        })
    return out


def active_chips(f: q.Filters) -> list[dict[str, str]]:
    """Включённые фильтры в виде чипов со ссылкой "убрать"."""
    chips: list[dict[str, str]] = []

    def add(label: str, **off: Any) -> None:
        chips.append({"label": label, "url": "/vacancies" + _qs(f.query_string(**off))})

    if f.q:
        add(f"Поиск: {f.q}", q="")
    if f.city:
        add(f.city, city="")
    if f.district:
        add(fmt.district_label(f.district), district="")
    if f.category:
        add(fmt.CATEGORY_LABELS.get(f.category, f.category), category="")
    if f.work_format:
        add(fmt.WORK_FORMAT_LABELS.get(f.work_format, f.work_format), work_format="")
    if f.pay_period:
        add("Оплата " + fmt.PERIOD_LABELS.get(f.pay_period, f.pay_period).replace("в месяц", "в месяц"), pay_period="")
    if f.no_experience:
        add("Без опыта", no_experience=False)
    if f.side_job:
        add("Подработка", side_job=False)
    if f.daily:
        add("Выплата каждый день", daily=False)
    if f.adult:
        add("Подходит с 18 лет", adult=False)
    if f.direct:
        add("От работодателя", direct=False)
    if f.pay_from:
        add(f"Оплата от {fmt.money(f.pay_from)} ₽", pay_from=0)
    return chips


def _qs(query: str) -> str:
    return "?" + query if query else ""


def filter_options(facets: dict[str, list[tuple[str, int]]], f: q.Filters) -> dict[str, list[tuple[str, str]]]:
    """Варианты для выпадающих списков: значение, подпись с числом. Выбранное значение всегда есть."""

    def build(pairs: list[tuple[str, int]], chosen: str, label: Callable[[str], str] = lambda s: s) -> list[tuple[str, str]]:
        out = [(value, f"{label(value)} ({n})") for value, n in pairs]
        if chosen and chosen not in {v for v, _ in pairs}:
            out.append((chosen, label(chosen)))
        return out

    cats = {name: n for name, n in facets["categories"]}
    ordered_cats = [(c, cats[c]) for c in CATEGORIES if c in cats]
    fmts = {name: n for name, n in facets["work_formats"]}
    ordered_fmts = [(w, fmts[w]) for w in WORK_FORMATS if w in fmts]
    periods = {name: n for name, n in facets["pay_periods"]}
    ordered_periods = [(p, periods[p]) for p in PAY_PERIODS if p in periods]
    return {
        "city": build(facets["cities"], f.city),
        "district": build(facets["districts"], f.district, fmt.district_label),
        "category": build(ordered_cats, f.category, lambda c: fmt.CATEGORY_LABELS.get(c, c)),
        "work_format": build(ordered_fmts, f.work_format, lambda c: fmt.WORK_FORMAT_LABELS.get(c, c)),
        "pay_period": build(ordered_periods, f.pay_period, lambda c: "Оплата " + fmt.PERIOD_LABELS.get(c, c)),
    }


app = create_app()
