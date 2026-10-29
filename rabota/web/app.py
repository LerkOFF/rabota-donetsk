"""Сайт "Работа Донецк": серверный рендер на FastAPI и Jinja2.

Запуск: .venv/bin/uvicorn rabota.web.app:app --port 8000
База берётся из rabota.db.connect (путь DB_PATH из .env), в тестах и для демо путь передаётся явно.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from rabota import config
from rabota.models import CATEGORIES, EMPLOYMENTS, PAY_PERIODS, WORK_FORMATS, Vacancy
from rabota.web import format as fmt
from rabota.web import queries as q

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

DEFAULT_SITE_URL = "https://работа-донецк.рф"
MAX_BODY = 32 * 1024

COMPLAINT_REASONS = (
    "Вакансия не актуальна",
    "Мошенничество или просьба заплатить за работу",
    "Это не вакансия: реклама или услуги",
    "Ошибка в данных: оплата, телефон, адрес",
    "Другое",
)

DEFAULT_RATE_LIMITS = {"complaint": (6, 3600), "post": (4, 3600)}


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
) -> FastAPI:
    """Собирает приложение. premoderate=True: вакансии из формы ждут проверки и в ленте не видны."""
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

    # журнал WAL: сборщики пишут, сайт читает без блокировок; если базу открыть нельзя, сайт всё равно стартует
    with contextlib.suppress(sqlite3.Error), db() as c:
        c.execute("PRAGMA journal_mode=WAL")

    def render(request: Request, name: str, context: dict[str, Any] | None = None, status: int = 200) -> Response:
        ctx: dict[str, Any] = {"path": request.url.path, "seo": {}}
        ctx.update(context or {})
        seo = ctx["seo"]
        seo.setdefault("title", "Работа Донецк: вакансии Донецка и ДНР")
        seo.setdefault("description", DEFAULT_DESCRIPTION)
        seo.setdefault("canonical", base_url + _quote_path(request.url.path))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

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

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=604800"
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return render(request, "error.html", {"code": 404, "seo": {"title": "Страница не найдена", "noindex": True}}, 404)
        if exc.status_code == 429:
            return render(request, "error.html", {"code": 429, "seo": {"title": "Слишком часто", "noindex": True}}, 429)
        return render(
            request, "error.html",
            {"code": exc.status_code, "detail": exc.detail, "seo": {"title": "Ошибка", "noindex": True}},
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        return render(request, "error.html", {"code": 500, "seo": {"title": "Ошибка на сайте", "noindex": True}}, 500)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

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
                "title": "Работа в Донецке и ДНР: свежие вакансии с оплатой и телефоном",
                "description": DEFAULT_DESCRIPTION,
                "jsonld": website_jsonld(base_url),
            },
        })

    @app.get("/vacancies")
    def vacancies(request: Request) -> Response:
        flt = q.Filters.from_params(request.query_params)
        page_no = q.parse_page(request.query_params.get("page"))
        with db() as conn:
            page = q.search(conn, flt, page_no)
            facets = q.facets(conn)
        items = [present(v) for v in page.items]
        title = "Вакансии в Донецке и ДНР"
        if flt.q:
            title = f"{flt.q.capitalize()}: вакансии в Донецке"
        elif flt.category:
            title = f"Вакансии: {fmt.CATEGORY_LABELS.get(flt.category, flt.category).lower()}"
        return render(request, "vacancies.html", {
            "flt": flt, "page": page, "items": items, "facets": facets,
            "chips": active_chips(flt),
            "options": filter_options(facets, flt),
            "base_qs": flt.query_string(),
            "seo": {
                "title": title + (f", страница {page.page}" if page.page > 1 else ""),
                "description": "Свежие вакансии Донецка и ДНР: оплата, график, район, телефон и ссылка на оригинал. "
                               "Фильтры по профессии, подработке, ежедневной выплате.",
                "noindex": not flt.is_empty,
                "canonical": base_url + "/vacancies" + (f"?page={page.page}" if page.page > 1 and flt.is_empty else ""),
                "prev_url": _page_url("/vacancies", flt.query_string(), page.page - 1) if page.has_prev else "",
                "next_url": _page_url("/vacancies", flt.query_string(), page.page + 1) if page.has_next else "",
            },
        })

    def listing(request: Request, flt: q.Filters, heading: str, kind: str, name: str, page_label: str) -> Response:
        page_no = q.parse_page(request.query_params.get("page"))
        with db() as conn:
            page = q.search(conn, flt, page_no)
            facets = q.facets(conn)
        if page.total == 0:
            raise StarletteHTTPException(404)
        base = request.url.path
        return render(request, "listing.html", {
            "heading": heading, "kind": kind, "name": name, "page": page,
            "items": [present(v) for v in page.items],
            "professions": [p for p in facets["professions"] if p[0] != name][:10],
            "districts": facets["districts"][:10],
            "seo": {
                "title": f"{heading}: {fmt.count_label(page.total, 'вакансия', 'вакансии', 'вакансий')}",
                "description": f"{heading}: {fmt.count_label(page.total, 'вакансия', 'вакансии', 'вакансий')} с оплатой, "
                               f"графиком и телефоном. {page_label}",
                "canonical": base_url + _quote_path(base) + (f"?page={page.page}" if page.page > 1 else ""),
                "prev_url": _page_url(base, "", page.page - 1) if page.has_prev else "",
                "next_url": _page_url(base, "", page.page + 1) if page.has_next else "",
            },
        })

    @app.get("/profession/{name}")
    def profession_page(request: Request, name: str) -> Response:
        name = name.strip()[:80]
        return listing(request, q.Filters(profession=name), f"{name.capitalize()}: вакансии в Донецке",
                       "profession", name.lower(), "Свежие предложения из открытых источников.")

    @app.get("/district/{name}")
    def district_page(request: Request, name: str) -> Response:
        name = name.strip()[:80]
        return listing(request, q.Filters(district=name), f"Работа: {fmt.district_label(name)}",
                       "district", name, "Вакансии рядом с домом.")

    @app.get("/city/{name}")
    def city_page(request: Request, name: str) -> Response:
        name = name.strip()[:80]
        return listing(request, q.Filters(city=name), f"Работа в городе {name}", "city", name,
                       "Вакансии города и окрестностей.")

    def vacancy_page_context(conn: Any, vacancy_id: int) -> dict[str, Any]:
        raw = q.get_vacancy(conn, vacancy_id)
        if raw is None or raw["kind"] != "vacancy" or raw["status"] not in ("active", "closed"):
            raise StarletteHTTPException(404)
        if raw["duplicate_of"]:
            main = q.get_vacancy(conn, raw["duplicate_of"])
            if main and q.is_listable(main):
                raise _Redirect(f"/vacancy/{main['id']}")
            raise StarletteHTTPException(404)
        closed = raw["status"] == "closed"
        v = present(raw)
        similar = [present(s) for s in q.similar(conn, raw)] if not closed else []
        also = present_also(q.also_published_in(conn, raw["id"]))
        return {"v": v, "closed": closed, "similar": similar, "also": also}

    class _Redirect(Exception):
        def __init__(self, url: str):
            self.url = url

    @app.exception_handler(_Redirect)
    async def redirect_handler(request: Request, exc: _Redirect) -> Response:
        return RedirectResponse(exc.url, status_code=301)

    def vacancy_seo(ctx: dict[str, Any]) -> dict[str, Any]:
        v = ctx["v"]
        where = v["location"] or "Донецк"
        title = f"{v['profession_title']}, {where}: {v['pay']['text']}"
        desc = f"{v['profession_title']} в {where}. {v['pay']['text']}."
        if v["schedule"]:
            desc += f" График {v['schedule']}."
        if v["summary"]:
            desc += " " + v["summary"]
        return {
            "title": title, "description": desc[:300], "og_type": "article",
            "noindex": ctx["closed"],
            "jsonld": None if ctx["closed"] else job_posting_jsonld(v, base_url),
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
        return render(request, "vacancy.html", ctx)

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
                if errors:
                    ctx.update({"errors": errors, "form": {"reason": reason, "comment": comment}})
                    return ctx
                text = reason + (f": {comment}" if comment else "")
                q.add_complaint(conn, vacancy_id, text)
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
            "categories": [(c, fmt.CATEGORY_LABELS[c]) for c in CATEGORIES],
            "work_formats": [(w, fmt.WORK_FORMAT_LABELS[w]) for w in WORK_FORMATS],
            "employments": [(e, fmt.EMPLOYMENT_LABELS[e]) for e in EMPLOYMENTS],
            "periods": [(p, fmt.PERIOD_LABELS[p]) for p in PAY_PERIODS],
            "seo": {
                "title": "Разместить вакансию в Донецке бесплатно",
                "description": "Опубликуйте вакансию на сайте: профессия, оплата, график, телефон. Бесплатно, после проверки.",
            },
        }

    @app.get("/razmestit")
    def post_form(request: Request) -> Response:
        return render(request, "post.html", post_context())

    @app.post("/razmestit")
    async def post_submit(request: Request) -> Response:
        if not same_origin(request):
            raise StarletteHTTPException(403, "Запрос с другого сайта")
        form = await read_form(request)
        if form is None:
            raise StarletteHTTPException(413, "Слишком большой запрос")
        if not limiter.allow("post", client_ip(request)):
            raise StarletteHTTPException(429)
        if form.get("website"):
            return RedirectResponse("/razmestit/spasibo", status_code=303)
        vacancy, cleaned, errors = validate_post(form)
        if errors or vacancy is None:
            ctx = post_context(cleaned, errors)
            ctx["seo"]["noindex"] = True
            return render(request, "post.html", ctx, 422)

        def save() -> int:
            with db() as conn:
                return q.add_site_vacancy(conn, vacancy, premoderate=premoderate)

        vacancy_id = await run_in_threadpool(save)
        target = "/razmestit/spasibo" if premoderate else f"/vacancy/{vacancy_id}"
        return RedirectResponse(target, status_code=303)

    @app.get("/razmestit/spasibo")
    def post_thanks(request: Request) -> Response:
        return render(request, "post_thanks.html", {"seo": {"title": "Вакансия отправлена", "noindex": True}})

    @app.get("/about")
    def about(request: Request) -> Response:
        return render(request, "about.html", {"seo": {
            "title": "О сайте Работа Донецк",
            "description": "Как мы собираем вакансии Донецка и ДНР, чем отличаемся от каналов и как не попасть на обман.",
        }})

    # ---------------------------------------------------------------- для поисковиков

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /razmestit/spasibo\n"
            "Disallow: /vacancy/*/complaint\n"
            f"Sitemap: {base_url}/sitemap.xml\n"
        )
        return PlainTextResponse(body)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=301)

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap() -> Response:
        with db() as conn:
            entries = q.sitemap_entries(conn)
        today = clock().date().isoformat()
        urls: list[tuple[str, str]] = [
            ("/", today), ("/vacancies", today), ("/razmestit", today), ("/about", today),
        ]
        urls += [(f"/profession/{quote(p.lower())}", today) for p in entries["professions"]]
        urls += [(f"/district/{quote(d)}", today) for d in entries["districts"]]
        urls += [(f"/city/{quote(c)}", today) for c in entries["cities"]]
        for vid, ts in entries["vacancies"]:
            dt = fmt.parse_dt(ts)
            urls.append((f"/vacancy/{vid}", (dt or clock()).date().isoformat()))
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for path, lastmod in urls:
            parts.append(f"<url><loc>{xml_escape(base_url + path)}</loc><lastmod>{lastmod}</lastmod></url>")
        parts.append("</urlset>")
        return Response("\n".join(parts), media_type="application/xml")

    return app


# -------------------------------------------------------------------- помощники

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


def website_jsonld(base_url: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "Работа Донецк",
        "url": base_url + "/",
        "inLanguage": "ru",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": base_url + "/vacancies?q={search_term_string}"},
            "query-input": "required name=search_term_string",
        },
    }


_EMPLOYMENT_TYPES = {
    "постоянно": "FULL_TIME", "подработка": "PART_TIME", "на сегодня": "TEMPORARY", "сменная": "OTHER",
}
_UNIT_TEXT = {"месяц": "MONTH", "смена": "DAY", "день": "DAY", "час": "HOUR", "задача": "DAY"}


def job_posting_jsonld(v: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Разметка JobPosting для поисковиков."""
    posted = fmt.parse_dt(v["published_at"] or v.get("created_at"))
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": v["profession_title"],
        "description": v["summary"] or v["title_raw"] or v["profession_title"],
        "identifier": {"@type": "PropertyValue", "name": "Работа Донецк", "value": str(v["id"])},
        "url": f"{base_url}/vacancy/{v['id']}",
        "hiringOrganization": {"@type": "Organization", "name": v["employer"] or "Работодатель"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": v["city"] or "Донецк",
                "addressRegion": "Донецкая Народная Республика",
                "addressCountry": "RU",
                **({"streetAddress": v["district"]} if v["district"] else {}),
            },
        },
    }
    if posted:
        ld["datePosted"] = posted.date().isoformat()
        ld["validThrough"] = (posted + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if v["employment"] in _EMPLOYMENT_TYPES:
        ld["employmentType"] = _EMPLOYMENT_TYPES[v["employment"]]
    if v["work_format"] == "удалённо":
        ld["jobLocationType"] = "TELECOMMUTE"
        ld["applicantLocationRequirements"] = {"@type": "Country", "name": "RU"}
    if v["pay_min"] or v["pay_max"]:
        value: dict[str, Any] = {"@type": "QuantitativeValue", "unitText": _UNIT_TEXT.get(v["pay_period"] or "", "MONTH")}
        if v["pay_min"] and v["pay_max"] and v["pay_min"] != v["pay_max"]:
            value["minValue"], value["maxValue"] = sorted((v["pay_min"], v["pay_max"]))
        else:
            value["value"] = v["pay_min"] or v["pay_max"]
        ld["baseSalary"] = {"@type": "MonetaryAmount", "currency": "RUB", "value": value}
    if v["experience"] and v["no_experience"]:
        ld["experienceRequirements"] = {"@type": "OccupationalExperienceRequirements", "monthsOfExperience": 0}
    if v["requirements"]:
        ld["qualifications"] = v["requirements"]
    return ld


# ---------------------------------------------------------------------- форма размещения

def _clean(form: dict[str, str], name: str, limit: int) -> str:
    return " ".join((form.get(name) or "").split())[:limit]


def _int(form: dict[str, str], name: str, errors: dict[str, str], lo: int, hi: int, label: str) -> int | None:
    raw = "".join(ch for ch in (form.get(name) or "") if ch.isdigit())
    if not raw:
        return None
    n = int(raw[:9])
    if not lo <= n <= hi:
        errors[name] = f"{label}: число от {lo} до {hi}."
        return None
    return n


def validate_post(form: dict[str, str]) -> tuple[Vacancy | None, dict[str, str], dict[str, str]]:
    """Проверка формы размещения. Возвращает вакансию, очищенные значения и ошибки по полям."""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {
        "profession": _clean(form, "profession", 80),
        "category": _clean(form, "category", 30),
        "work_format": _clean(form, "work_format", 30),
        "employment": _clean(form, "employment", 30),
        "schedule": _clean(form, "schedule", 60),
        "pay_min": _clean(form, "pay_min", 9),
        "pay_max": _clean(form, "pay_max", 9),
        "pay_period": _clean(form, "pay_period", 20),
        "pay_daily": "1" if form.get("pay_daily") else "",
        "no_experience": "1" if form.get("no_experience") else "",
        "city": _clean(form, "city", 60) or "Донецк",
        "district": _clean(form, "district", 60),
        "age_min": _clean(form, "age_min", 3),
        "requirements": _clean(form, "requirements", 400),
        "employer": _clean(form, "employer", 120),
        "phone": _clean(form, "phone", 30),
        "telegram": _clean(form, "telegram", 64),
        "email": _clean(form, "email", 120),
        "summary": " ".join((form.get("summary") or "").split())[:1000],
    }
    if len(cleaned["profession"]) < 3:
        errors["profession"] = "Укажите профессию: хотя бы три буквы."
    if cleaned["category"] not in CATEGORIES:
        errors["category"] = "Выберите сферу из списка."
    if cleaned["work_format"] and cleaned["work_format"] not in WORK_FORMATS:
        errors["work_format"] = "Выберите формат из списка."
    if cleaned["employment"] and cleaned["employment"] not in EMPLOYMENTS:
        errors["employment"] = "Выберите тип занятости из списка."
    if cleaned["pay_period"] and cleaned["pay_period"] not in PAY_PERIODS:
        errors["pay_period"] = "Выберите период оплаты из списка."
    pay_min = _int(form, "pay_min", errors, 1, 5_000_000, "Оплата")
    pay_max = _int(form, "pay_max", errors, 1, 5_000_000, "Оплата")
    if (pay_min or pay_max) and not cleaned["pay_period"] and "pay_period" not in errors:
        errors["pay_period"] = "Укажите, за что платите: за месяц, смену, день или час."
    if pay_min and pay_max and pay_min > pay_max:
        errors["pay_max"] = "Максимум не может быть меньше минимума."
    age_min = _int(form, "age_min", errors, 14, 80, "Возраст")

    phone = ""
    if cleaned["phone"]:
        norm = fmt.normalize_phone(cleaned["phone"])
        if norm is None:
            errors["phone"] = "Телефон в формате +7 949 123-45-67."
        else:
            phone = norm
            cleaned["phone"] = norm
    telegram = cleaned["telegram"]
    if telegram and not _valid_telegram(telegram):
        errors["telegram"] = "Ник Telegram вида @name: латиница, цифры, подчёркивание."
    email = cleaned["email"]
    if email and ("@" not in email or " " in email or "." not in email.rsplit("@", 1)[-1]):
        errors["email"] = "Проверьте адрес почты."
    if not (phone or telegram or email) and "phone" not in errors and "telegram" not in errors:
        errors["phone"] = "Оставьте телефон или ник Telegram, иначе соискатель не сможет связаться."
    if len(cleaned["summary"]) < 20:
        errors["summary"] = "Опишите работу в паре предложений: хотя бы 20 символов."
    if errors:
        return None, cleaned, errors

    contacts: dict[str, Any] = {}
    if phone:
        contacts["phones"] = [phone]
    if telegram:
        contacts["telegram"] = telegram if telegram.startswith("@") else "@" + telegram
    if email:
        contacts["email"] = email
    vacancy = Vacancy(
        profession=cleaned["profession"].lower(),
        title_raw=cleaned["profession"],
        category=cleaned["category"],
        work_format=cleaned["work_format"] or None,
        employment=cleaned["employment"] or None,
        schedule=cleaned["schedule"] or None,
        pay_min=pay_min,
        pay_max=pay_max,
        pay_period=cleaned["pay_period"] or None,
        pay_daily=bool(cleaned["pay_daily"]),
        city=cleaned["city"],
        district=cleaned["district"] or None,
        experience="не нужен" if cleaned["no_experience"] else None,
        age_min=age_min,
        requirements=cleaned["requirements"] or None,
        employer=cleaned["employer"] or None,
        contacts=contacts,
        summary=cleaned["summary"],
    )
    return vacancy, cleaned, errors


def _valid_telegram(value: str) -> bool:
    v = value.lstrip("@")
    return 4 <= len(v) <= 32 and all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in v)


app = create_app()
