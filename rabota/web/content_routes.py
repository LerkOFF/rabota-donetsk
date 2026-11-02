"""Контент и установка на телефон: зарплаты, подборки, блог, подсказки поиска, manifest и service worker.

Подключение: `content_routes.install(app, render=render, templates=templates, db_path=..., site_url=..., clock=...)`
из create_app. Модуль не импортирует rabota.web.app (круговых импортов нет). Правила содержания: docs/CONTENT.md,
индексация и разметка: docs/SEO.md.

Маршруты:
    /zarplaty, /zarplaty/<профессия>      зарплаты по данным сайта
    /podborki, /podborki/<подборка>[/<район или профессия>]
    /blog, /blog/<статья>
    /suggest                              подсказки строки поиска (JSON, кэш в памяти)
    /manifest.webmanifest, /sw.js, /offline
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from rabota.web import articles, pwa, salary, selections
from rabota.web import format as fmt
from rabota.web import queries as q
from rabota.web import seo as seolib

CACHE_SECONDS = 60  # тесты ставят 0
MIN_SALARY_INDEX = 3  # /zarplaty индексируется, если профессий с достаточным числом вакансий не меньше
SUGGEST_LIMIT = 8
SALARY_PUBLISHED = "2026-11-02"  # дата запуска раздела, для разметки Article


class Ttl:
    """Маленький кэш значения на несколько секунд: подсказки и счётчики не должны ходить в базу на каждый запрос."""

    def __init__(self) -> None:
        self._at = -1e9
        self._value: Any = None

    def get(self, build: Callable[[], Any]) -> Any:
        if CACHE_SECONDS <= 0 or time.monotonic() - self._at > CACHE_SECONDS:
            self._value = build()
            self._at = time.monotonic()
        return self._value


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _page_url(base: str, page: int) -> str:
    return base + (f"?page={page}" if page > 1 else "")


# ------------------------------------------------------------------ зарплаты

def salary_block(conn: sqlite3.Connection, profession: str, today: datetime) -> dict[str, Any] | None:
    """Блок «Зарплаты» для страницы профессии. None, если вакансий с оплатой меньше порога."""
    stats = salary.stats_for(conn, profession)
    main = salary.best(stats)
    if not main:
        return None
    return {"profession": profession, "stats": stats, "main": main, "date": today.strftime("%d.%m.%Y"),
            "href": f"/zarplaty/{quote(profession.lower(), safe='')}"}


def listing_extras(conn: sqlite3.Connection, kind: str, name: str, today: datetime) -> dict[str, Any]:
    """Добавки к странице-посадке: блок зарплат (для профессий) и группа ссылок на подборки в этой профессии или районе."""
    block = salary_block(conn, name, today) if kind == "profession" else None
    groups: list[dict[str, Any]] = []
    if kind in ("profession", "district"):
        scope = (kind, name.lower() if kind == "profession" else name)
        totals = selections.counts(conn, scope)
        whole = selections.count(conn, None, scope)
        value = quote(scope[1], safe="")
        links = [
            {"label": f"{_cap(name)}: {s.short}" if kind == "profession" else s.nav,
             "href": f"/podborki/{s.slug}/{value}", "n": totals[s.slug], "icon": False}
            for s in selections.SELECTIONS if selections.SELECTION_MIN <= totals[s.slug] < whole
        ]
        if links:
            groups.append({"title": "Подборки по профессии" if kind == "profession" else "Подборки в этом районе",
                           "items": links})
    return {"salary": block, "groups": groups}


# ------------------------------------------------------------------ подсказки

def build_suggest_data(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    facets = q.facets(conn)
    districts = []
    for name, n in facets["districts"]:
        districts.append({"label": fmt.district_label(name), "value": fmt.district_label(name), "kind": "district", "n": n,
                          "href": f"/district/{quote(name, safe='')}"})
    cities = [{"label": name, "value": name, "kind": "city", "n": n, "href": f"/city/{quote(name, safe='')}"}
              for name, n in facets["cities"]]
    professions = [{"label": _cap(name), "value": _cap(name), "kind": "profession", "n": n,
                    "href": f"/profession/{quote(name.lower(), safe='')}"} for name, n in facets["professions"]]
    return {"q": professions, "where": [*cities, *districts]}


def suggest(data: dict[str, list[dict[str, Any]]], field: str, text: str, limit: int = SUGGEST_LIMIT) -> list[dict[str, Any]]:
    """Подходящие подсказки: сначала начинающиеся с запроса (или с одного из слов), затем содержащие, по числу вакансий."""
    pool = data.get("where" if field == "where" else "q", [])
    needle = q.fold(" ".join(text.split()))[:40]
    if not needle:
        return sorted(pool, key=lambda x: (-x["n"], x["label"]))[:limit]
    starts, inside = [], []
    for item in pool:
        label = q.fold(item["label"])
        words = label.replace("-", " ").split()
        if label.startswith(needle) or any(w.startswith(needle) for w in words):
            starts.append(item)
        elif needle in label:
            inside.append(item)
    def order(x: dict[str, Any]) -> tuple[int, str]:
        return (-x["n"], x["label"])

    return (sorted(starts, key=order) + sorted(inside, key=order))[:limit]


# ------------------------------------------------------------------ карта сайта

def sitemap_groups(conn: sqlite3.Connection, latest: str, now: datetime) -> dict[str, list[tuple[str, str]]]:
    """Адреса контентных страниц для карты сайта: только индексируемые. latest: дата последней вакансии (ГГГГ-ММ-ДД)."""
    items = articles.all_articles()
    static: list[tuple[str, str]] = []
    if items:
        static.append(("/blog", max(a.updated for a in items).isoformat()))
    pages = selections.index_pages(conn)
    if pages["base"]:
        static.append(("/podborki", latest))
    profs = salary.all_professions(conn)
    if len(profs) >= MIN_SALARY_INDEX:
        static.append(("/zarplaty", latest))
    return {
        "static": static,
        "selections": [(p, latest) for kind in ("base", "district", "profession") for p, _, _ in pages[kind]],
        "salary": [(f"/zarplaty/{e['name']}", seolib.date_of(e["last"], now)) for e in profs],
        "articles": [(a.path, a.updated.isoformat()) for a in items],
    }


def index_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Сколько страниц в индексе по типам: для админки. Каждая строка: ключ, название, число, пояснение."""
    conn.create_function("rfold", 1, lambda s: q.fold(s) if isinstance(s, str) else s, deterministic=True)
    pages = selections.index_pages(conn)
    entries = q.sitemap_entries(conn, seolib.MIN_INDEXABLE)
    profs = salary.all_professions(conn)
    n_articles = len(articles.all_articles())
    return [
        {"key": "selections", "title": "Подборки", "n": sum(len(v) for v in pages.values()),
         "parts": [("главные", len(pages["base"])), ("по районам", len(pages["district"])), ("по профессиям", len(pages["profession"]))],
         "note": f"Порог индексации: {selections.SELECTION_MIN} вакансий и меньше всей выдачи района или профессии.",
         "href": "/podborki"},
        {"key": "professions", "title": "Профессии", "n": len(entries["professions"]), "parts": [],
         "note": f"Страницы /profession/ с {seolib.MIN_INDEXABLE} вакансиями и больше.", "href": "/vacancies"},
        {"key": "salary", "title": "Зарплаты", "n": len(profs) + (1 if len(profs) >= MIN_SALARY_INDEX else 0), "parts": [],
         "note": f"Профессии, где не меньше {salary.MIN_SAMPLE} вакансий с оплатой, и общая страница.", "href": "/zarplaty"},
        {"key": "articles", "title": "Статьи", "n": n_articles + (1 if n_articles else 0), "parts": [],
         "note": "Статьи из docs/articles и страница блога.", "href": "/blog"},
        {"key": "places", "title": "Районы и города", "n": len(entries["districts"]) + len(entries["cities"]), "parts": [],
         "note": f"Страницы /district/ и /city/ с {seolib.MIN_INDEXABLE} вакансиями и больше.", "href": "/vacancies"},
    ]


# ------------------------------------------------------------------ подключение

def install(
    app: FastAPI, *, render: Callable[..., Response], templates: Any, db_path: Any, site_url: str,
    clock: Callable[[], datetime], present: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    base_url = site_url.rstrip("/")
    env = templates.env
    suggest_cache, chips_cache = Ttl(), Ttl()

    def db() -> Any:
        return q.open_conn(db_path)

    def chips() -> list[dict[str, Any]]:
        def build() -> list[dict[str, Any]]:
            try:
                with db() as conn:
                    return selections.quick_chips(conn)
            except sqlite3.Error:
                return []
        return chips_cache.get(build)

    env.globals["quick_selections"] = chips
    env.globals["latest_articles"] = lambda n=3: list(articles.all_articles()[:n])

    def today() -> datetime:
        return clock().astimezone(UTC)

    # ---------------------------------------------------------- зарплаты

    @app.get("/zarplaty")
    def salary_index(request: Request) -> Response:
        with db() as conn:
            items = salary.all_professions(conn)
        now = today()
        indexable = len(items) >= MIN_SALARY_INDEX
        top = items[0] if items else None
        desc = ("Сколько платят в Донецке: медиана и вилка зарплаты по профессиям, посчитанные по вакансиям сайта "
                f"с указанной оплатой на {now.strftime('%d.%m.%Y')}.")
        ld = {
            "@context": "https://schema.org", "@type": "Article",
            "headline": "Зарплаты в Донецке по профессиям", "description": desc, "inLanguage": "ru",
            "datePublished": SALARY_PUBLISHED, "dateModified": now.date().isoformat(),
            "mainEntityOfPage": base_url + "/zarplaty", "image": base_url + "/static/og.png",
            "author": {"@type": "Organization", "name": seolib.SITE_NAME, "url": base_url + "/"},
            "publisher": {"@id": base_url + "/#organization"},
        }
        return render(request, "salary_index.html", {
            "items": items, "date": now.strftime("%d.%m.%Y"), "top": top, "min_sample": salary.MIN_SAMPLE,
            "articles": articles.all_articles()[:3],
            "seo": {
                "title": "Зарплаты в Донецке по профессиям",
                "description": desc, "noindex": not indexable, "og_type": "article",
                "breadcrumbs": [("Главная", "/"), ("Зарплаты", "/zarplaty")],
                "jsonld": ld,
            },
        })

    @app.get("/zarplaty/{profession}")
    def salary_page(request: Request, profession: str) -> Response:
        with db() as conn:
            name = q.canonical_name(conn, "profession", profession.strip()[:80])
            if not name:
                raise StarletteHTTPException(404)
            name = name.lower()
            stats = salary.stats_for(conn, name)
            main = salary.best(stats)
            others = [e for e in salary.all_professions(conn) if e["name"] != name][:8]
            sample = [present(v) for v in q.search(conn, q.Filters(profession=name), 1).items if v["pay_min"] or v["pay_max"]][:4]
            has_page = q.search(conn, q.Filters(profession=name), 1).total >= seolib.MIN_INDEXABLE
        now = today()
        path = f"/zarplaty/{name}"
        faq = salary.faq_for(name, stats, now)
        label = _cap(name)
        ld: list[dict[str, Any]] = []
        if main:
            ld.append({
                "@context": "https://schema.org", "@type": "Article",
                "headline": f"Зарплата: {label} в Донецке", "description": salary.description_for(name, main, now),
                "inLanguage": "ru", "datePublished": SALARY_PUBLISHED, "dateModified": now.date().isoformat(),
                "mainEntityOfPage": base_url + quote(path, safe="/%"), "image": base_url + "/static/og.png",
                "author": {"@type": "Organization", "name": seolib.SITE_NAME, "url": base_url + "/"},
                "publisher": {"@id": base_url + "/#organization"},
            })
            ld.append(seolib.faq_jsonld(faq))
        return render(request, "salary.html", {
            "name": name, "label": label, "stats": stats, "main": main, "faq": faq, "others": others, "sample": sample,
            "has_page": has_page, "date": now.strftime("%d.%m.%Y"), "min_sample": salary.MIN_SAMPLE,
            "articles": articles.all_articles()[:3],
            "seo": {
                "title": salary.title_for(name, main),
                "description": salary.description_for(name, main, now),
                "noindex": main is None, "og_type": "article",
                "canonical": seolib.path_url(base_url, path),
                "breadcrumbs": [("Главная", "/"), ("Зарплаты", "/zarplaty"), (label, path)],
                "jsonld": ld,
            },
        })

    # ---------------------------------------------------------- подборки

    @app.get("/podborki")
    def selections_index(request: Request) -> Response:
        with db() as conn:
            totals = selections.counts(conn)
            all_total = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {q.VISIBLE_SQL}").fetchone()[0])
            pages = selections.index_pages(conn)
        cards = [{"sel": s, "n": totals[s.slug]} for s in selections.SELECTIONS]
        indexable = bool(pages["base"])
        return render(request, "selections_index.html", {
            "cards": cards, "all_total": all_total,
            "district_links": [(quote(p, safe="/%"), n, label) for p, n, label in pages["district"][:12]],
            "profession_links": [(quote(p, safe="/%"), n, label) for p, n, label in pages["profession"][:12]],
            "seo": {
                "title": "Подборки вакансий в Донецке",
                "description": "Готовые подборки вакансий Донецка и ДНР: подработка на сегодня, ежедневная выплата, "
                               "работа без опыта, для студентов, с 18 лет, удалённая работа и вахта.",
                "noindex": not indexable,
                "breadcrumbs": [("Главная", "/"), ("Подборки", "/podborki")],
            },
        })

    def selection_response(request: Request, slug: str, raw_scope: str | None) -> Response:
        sel = selections.get(slug)
        if sel is None:
            raise StarletteHTTPException(404)
        page_no = q.parse_page(request.query_params.get("page"))
        with db() as conn:
            scope = None
            if raw_scope is not None:
                scope = selections.resolve_scope(conn, raw_scope)
                if scope is None:
                    raise StarletteHTTPException(404)
            page = selections.page(conn, sel, scope, page_no)
            if scope and page.total == 0:
                raise StarletteHTTPException(404)
            whole = selections.count(conn, None, scope)
            facts = selections.facts(conn, sel, scope)
            totals = selections.counts(conn, scope)
            district_combos = [] if scope else selections.combos(conn, sel, "district")
            profession_combos = [] if scope else selections.combos(conn, sel, "profession")
            salary_href = ""
            if scope and scope[0] == "profession" and salary.stats_for(conn, scope[1]):
                salary_href = f"/zarplaty/{quote(scope[1], safe='')}"
        indexable = selections.is_indexable(page.total, whole if scope else None, whole)
        path = f"/podborki/{slug}" + (f"/{scope[1]}" if scope else "")
        quoted = quote(path, safe="/%")
        items = [present(v) for v in page.items]
        title = selections.title_key(sel, scope, page.total) + (f", страница {page.page}" if page.page > 1 else "")
        desc = (f"Страница {page.page} из {page.pages}. " if page.page > 1 else "") \
            + selections.description(sel, scope, page.total, facts)
        crumbs: list[tuple[str, str]] = [("Главная", "/"), ("Подборки", "/podborki")]
        if scope:
            crumbs += [(sel.heading, f"/podborki/{slug}"), (selections.scope_label(scope), path)]
        else:
            crumbs.append((sel.heading, path))
        suffix = f"/{quote(scope[1], safe='')}" if scope else ""
        others = [
            {"label": s.nav, "href": f"/podborki/{s.slug}{suffix}", "n": totals[s.slug], "icon": False}
            for s in selections.SELECTIONS
            if s.slug != slug and (totals[s.slug] >= selections.SELECTION_MIN if scope else totals[s.slug] > 0)
        ]
        groups: list[dict[str, Any]] = []
        if others:
            groups.append({"title": "Другие подборки" + (f": {selections.scope_label(scope)}" if scope else ""), "items": others})
        if district_combos:
            groups.append({"title": "Вакансии по районам", "items": [
                {"label": fmt.district_label(v), "href": f"/podborki/{slug}/{quote(v, safe='')}", "n": n, "icon": True}
                for v, n in district_combos[:12]]})
        if profession_combos:
            groups.append({"title": "Вакансии по профессиям", "items": [
                {"label": _cap(v), "href": f"/podborki/{slug}/{quote(v, safe='')}", "n": n, "icon": False}
                for v, n in profession_combos[:12]]})
        if scope:
            kind, v = scope
            label = fmt.district_label(v) if kind == "district" else _cap(v)
            groups.append({"title": "Район целиком" if kind == "district" else "Профессия целиком", "items": [
                {"label": label, "href": f"/{kind}/{quote(v, safe='')}", "n": whole, "icon": kind == "district"}]})
        return render(request, "selection.html", {
            "sel": sel, "scope": scope, "heading": selections.heading(sel, scope), "page": page, "items": items,
            "intro": selections.lead_paragraphs(sel, scope, page.total, facts), "groups": groups,
            "related": [a for a in (articles.get(s) for s in sel.articles) if a], "salary_href": salary_href,
            "list_path": quoted,
            "seo": {
                "title": title, "description": desc, "noindex": not indexable,
                "canonical": seolib.path_url(base_url, path, f"page={page.page}" if page.page > 1 else ""),
                "prev_url": _page_url(quoted, page.page - 1) if page.has_prev else "",
                "next_url": _page_url(quoted, page.page + 1) if page.has_next else "",
                "breadcrumbs": crumbs,
                "jsonld": seolib.item_list_jsonld(base_url, items, (page.page - 1) * page.per_page),
            },
        })

    @app.get("/podborki/{slug}")
    def selection_page(request: Request, slug: str) -> Response:
        return selection_response(request, slug, None)

    @app.get("/podborki/{slug}/{scope}")
    def selection_scope_page(request: Request, slug: str, scope: str) -> Response:
        return selection_response(request, slug, scope)

    # ---------------------------------------------------------- блог

    @app.get("/blog")
    def blog_index(request: Request) -> Response:
        items = articles.all_articles()
        return render(request, "blog_index.html", {
            "articles": items,
            "seo": {
                "title": "Блог о работе в Донецке: статьи и советы",
                "description": "Как составить резюме, пройти собеседование, отличить мошенников, оформиться и выбрать график: "
                               "советы для тех, кто ищет работу в Донецке и ДНР.",
                "noindex": not items,
                "breadcrumbs": [("Главная", "/"), ("Блог", "/blog")],
            },
        })

    @app.get("/blog/{slug}")
    def blog_article(request: Request, slug: str) -> Response:
        article = articles.get(slug)
        if article is None:
            raise StarletteHTTPException(404)
        more = articles.related(article)
        return render(request, "blog_article.html", {
            "a": article, "more": more,
            "seo": {
                "title": article.title, "description": article.description, "og_type": "article",
                "breadcrumbs": [("Главная", "/"), ("Блог", "/blog"), (article.title, article.path)],
                "jsonld": articles.article_jsonld(article, base_url, base_url + "/#organization", base_url + "/static/og.png"),
            },
        })

    # ---------------------------------------------------------- подсказки поиска

    @app.get("/suggest", include_in_schema=False)
    def suggest_view(request: Request) -> Response:
        field = "where" if request.query_params.get("f") == "where" else "q"
        text = request.query_params.get("q", "")[:60]

        def build() -> dict[str, list[dict[str, Any]]]:
            try:
                with db() as conn:
                    return build_suggest_data(conn)
            except sqlite3.Error:
                return {"q": [], "where": []}

        data = suggest_cache.get(build)
        items = [{"label": i["label"], "value": i["value"], "kind": i["kind"], "n": i["n"]} for i in suggest(data, field, text)]
        return JSONResponse({"items": items}, headers={"Cache-Control": "public, max-age=60", "X-Robots-Tag": "noindex"})

    # ---------------------------------------------------------- установка на телефон

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest_view() -> Response:
        body = json.dumps(pwa.manifest(), ensure_ascii=False, indent=2)
        return Response(body, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/sw.js", include_in_schema=False)
    def sw_view() -> Response:
        # без кэша: браузер сам проверяет service worker не реже раза в сутки, а нам нужно, чтобы правки доходили сразу
        return Response(pwa.render_sw(), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/offline")
    def offline(request: Request) -> Response:
        # страницу кладёт в кэш service worker: личных данных в ней быть не должно, поэтому пользователь не передаётся
        return render(request, "offline.html", {"user": None, "csrf_token": "", "seo": {
            "title": "Нет подключения к сети", "noindex": True,
            "description": "Страница недоступна без интернета. Проверьте подключение и обновите страницу: вакансии откроются снова.",
        }})
