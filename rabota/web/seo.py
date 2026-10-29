"""SEO сайта: сборка словаря `seo` для шаблонов, разметка schema.org, карта сайта, robots.txt, заголовки.

Подробности и правила: docs/SEO.md.

Любая страница передаёт в шаблон словарь `seo` с любыми из ключей `title`, `description`, `noindex`, `og_type`,
`jsonld`, `breadcrumbs`, `canonical`. Дополняет и проверяет его `build_seo` (его вызывают функции render в
app.py и legal_routes.py), поэтому новой странице достаточно указать title и description:

    render(request, "page.html", {"seo": {"title": "Заголовок", "description": "Описание", "noindex": True}})

Модуль не зависит от FastAPI-приложения: его можно подключать из любого роутера.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from markupsafe import Markup

from rabota import config
from rabota.web import format as fmt

SITE_NAME = "Работа Донецк"
TITLE_SEP = " | "
TITLE_LIMIT = 60  # знаков в заголовке, включая название сайта
DESC_MIN, DESC_MAX = 110, 160
MIN_INDEXABLE = 3  # выдача с меньшим числом вакансий получает noindex и не попадает в карту сайта
SITEMAP_CHUNK = 5000
SITEMAP_CACHE_SECONDS = 3600
DEFAULT_IMAGE = "/static/og.png"

# Служебные разделы: на них всегда noindex, даже если страница забыла его указать
PRIVATE_PREFIXES = (
    "/login", "/register", "/logout", "/verify", "/forgot", "/reset", "/account", "/cabinet", "/email",
    "/admin", "/messages", "/notifications", "/responses", "/razmestit/spasibo",
)
PRIVATE_PATTERNS = (re.compile(r"^/vacancy/[^/]+/(complaint|apply|respond|favorite)(/|$)"),)

DESC_PADS = (
    "Вакансии Донецка и ДНР с оплатой, графиком, районом и телефоном.",
    "Ссылка на оригинал в каждой вакансии.",
)

HOME_DESCRIPTION = (
    "Вакансии Донецка и ДНР в одном месте: бариста, кассир, водитель, грузчик, подработка на смену. "
    "Оплата, график, район и телефон, ссылка на оригинал."
)


# ------------------------------------------------------------------ тексты

def clean(text: str | None) -> str:
    """Одна строка: пробелы, переносы и неразрывные пробелы схлопнуты."""
    return " ".join(str(text or "").split())


def truncate(text: str, limit: int) -> str:
    """Обрезка по границе слова с многоточием. Короткий текст возвращается как есть."""
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-.") + "…"


def compose_title(head: str, *, brand: bool = True, limit: int = TITLE_LIMIT) -> str:
    """"Ключ | Работа Донецк". Название сайта добавляется, если укладывается в limit; длинный ключ обрезается."""
    head = clean(head)
    if not head:
        return SITE_NAME
    if SITE_NAME in head or not brand:
        return truncate(head, limit + 8 if SITE_NAME in head else limit)
    suffix = TITLE_SEP + SITE_NAME
    if len(head) + len(suffix) <= limit:
        return head + suffix
    return truncate(head, limit)


def fit_description(text: str, pads: Sequence[str] = DESC_PADS) -> str:
    """Описание 110-160 знаков: длинное режется по предложению или слову, короткое дополняется общими фразами."""
    text = clean(text)
    if len(text) > DESC_MAX:
        window = text[:DESC_MAX]
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", window)]
        good = [e for e in ends if e >= DESC_MIN]
        text = window[: good[-1]].strip() if good else truncate(text, DESC_MAX)
    for pad in pads:
        if len(text) >= DESC_MIN:
            break
        if len(text) + 1 + len(pad) <= DESC_MAX:
            text = f"{text} {pad}".strip()
    return text


# ------------------------------------------------------------------ адреса

def path_url(site_url: str, path: str, query: str = "") -> str:
    """Абсолютный адрес: путь с кириллицей кодируется, query уже собран вызывающим."""
    url = site_url.rstrip("/") + quote(path or "/", safe="/%")
    return url + ("?" + query.lstrip("?") if query else "")


def is_private_path(path: str) -> bool:
    for prefix in PRIVATE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return any(p.match(path) for p in PRIVATE_PATTERNS)


def robots_value(noindex: bool) -> str:
    return "noindex, follow" if noindex else ""


# ------------------------------------------------------------------ сборка словаря seo

def _crumb_list(site_url: str, breadcrumbs: Iterable[Any] | None, current_path: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in breadcrumbs or ():
        if isinstance(item, dict):
            name, path = item.get("name", ""), item.get("path", "")
        else:
            name, path = item
        name = clean(name)
        if not name:
            continue
        path = path or current_path
        out.append({"name": name, "path": path, "href": quote(path, safe="/%"), "url": path_url(site_url, path)})
    return out


def breadcrumb_jsonld(crumbs: Sequence[dict[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": c["name"], "item": c["url"]}
            for i, c in enumerate(crumbs, 1)
        ],
    }


def script_json(data: Any) -> Markup:
    """JSON для <script type="application/ld+json">: кириллица как есть, закрыть тег изнутри нельзя."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    raw = raw.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(raw)


def build_seo(
    site_url: str,
    path: str,
    *,
    title: str = "",
    description: str = "",
    canonical: str | None = None,
    noindex: bool = False,
    og_type: str = "website",
    jsonld: Any = None,
    breadcrumbs: Iterable[Any] | None = None,
    prev_url: str = "",
    next_url: str = "",
    image: str = "",
    brand: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Полный словарь seo для base.html.

    title: ключевая часть, название сайта добавится само; description: любой длины, подгоняется под 110-160;
    canonical: абсолютный адрес, по умолчанию адрес текущей страницы; noindex: закрыть страницу от индекса
    (служебные разделы закрываются сами); jsonld: словарь или список словарей schema.org, первым идёт
    главный объект страницы; breadcrumbs: [(название, путь)], последний пункт - сама страница (путь можно "").
    """
    site_url = site_url.rstrip("/")
    noindex = bool(noindex) or is_private_path(path)
    crumbs = _crumb_list(site_url, breadcrumbs, path)
    blocks: list[Any] = []
    if isinstance(jsonld, dict):
        blocks.append(jsonld)
    elif jsonld:
        blocks.extend(x for x in jsonld if x)
    if noindex:
        blocks = []  # закрытая страница разметку не получает
    elif crumbs:
        blocks.append(breadcrumb_jsonld(crumbs))
    final_title = compose_title(title or SITE_NAME, brand=brand)
    final_desc = fit_description(description or HOME_DESCRIPTION)
    seo: dict[str, Any] = {
        "title": final_title,
        "description": final_desc,
        "canonical": canonical or path_url(site_url, path),
        "noindex": noindex,
        "robots": robots_value(noindex),
        "og_type": og_type or "website",
        "og_image": image if image.startswith("http") else site_url + (image or DEFAULT_IMAGE),
        "jsonld": blocks,
        "jsonld_html": [script_json(b) for b in blocks],
        "breadcrumbs": crumbs,
        "prev_url": prev_url or "",
        "next_url": next_url or "",
    }
    seo.update(extra)
    return seo


def finalize(seo: dict[str, Any] | None, site_url: str, path: str) -> dict[str, Any]:
    """Достраивает словарь seo, который собрал маршрут (в нём могут быть только title и description)."""
    data = dict(seo or {})
    if data.get("_final"):
        return data
    result = build_seo(site_url, path, **{k: v for k, v in data.items() if k not in ("robots", "jsonld_html", "og_image")})
    result["_final"] = True
    return result


def apply_headers(response: Any, seo: dict[str, Any]) -> None:
    """X-Robots-Tag дублирует meta robots: его видят и роботы, которые не разбирают HTML."""
    if seo.get("noindex"):
        response.headers["X-Robots-Tag"] = "noindex, follow"


# ------------------------------------------------------------------ schema.org

def website_jsonld(site_url: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": site_url + "/",
        "inLanguage": "ru",
        "publisher": {"@id": site_url + "/#organization"},
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": site_url + "/vacancies?q={search_term_string}"},
            "query-input": "required name=search_term_string",
        },
    }


def organization_jsonld(site_url: str, email: str = "") -> dict[str, Any]:
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": site_url + "/#organization",
        "name": SITE_NAME,
        "url": site_url + "/",
        "logo": site_url + "/static/favicon.svg",
        "description": "Вакансии Донецка и ДНР: от работодателей на сайте и из открытых источников.",
        "areaServed": {"@type": "AdministrativeArea", "name": "Донецк, Донецкая Народная Республика"},
    }
    if email:
        ld["contactPoint"] = {"@type": "ContactPoint", "contactType": "customer support", "email": email,
                              "availableLanguage": "ru"}
    return ld


def company_jsonld(site_url: str, company: dict[str, Any]) -> dict[str, Any]:
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": company["name"],
        "url": f"{site_url}/company/{company['id']}",
        "address": {"@type": "PostalAddress", "addressLocality": company.get("city") or "Донецк", "addressCountry": "RU"},
    }
    if company.get("description"):
        ld["description"] = truncate(company["description"], 300)
    site = company.get("site") or ""
    if site.startswith(("http://", "https://")):
        ld["sameAs"] = [site]
    return ld


def item_list_jsonld(site_url: str, items: Sequence[dict[str, Any]], offset: int = 0) -> dict[str, Any]:
    """Список вакансий выдачи: position продолжает нумерацию страниц."""
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem", "position": offset + i,
                "url": f"{site_url}/vacancy/{v['id']}", "name": v.get("profession_title") or v.get("profession") or "",
            }
            for i, v in enumerate(items, 1)
        ],
    }


def faq_jsonld(faq: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq
        ],
    }


EMPLOYMENT_TYPES = {"постоянно": "FULL_TIME", "подработка": "PART_TIME", "на сегодня": "TEMPORARY", "сменная": "OTHER"}
UNIT_TEXT = {"месяц": "MONTH", "смена": "DAY", "день": "DAY", "час": "HOUR", "задача": "DAY"}


def valid_through(v: dict[str, Any], ttl_days: int | None = None) -> datetime | None:
    """Срок действия: последняя публикация плюс срок жизни вакансии (по умолчанию 21 день)."""
    ttl = config.VACANCY_TTL_DAYS if ttl_days is None else ttl_days
    last = fmt.parse_dt(v.get("last_seen_at")) or fmt.parse_dt(v.get("published_at") or v.get("created_at"))
    return last + timedelta(days=ttl) if last else None


def job_description_html(v: dict[str, Any]) -> str:
    """Описание для JobPosting: Google просит HTML, текст экранируется."""
    parts = [f"<p>{xml_escape(clean(v['summary'] or v['title_raw'] or v['profession_title']))}</p>"]
    facts = []
    if v.get("schedule"):
        facts.append(f"График: {clean(v['schedule'])}")
    if v.get("requirements"):
        facts.append(f"Требования: {clean(v['requirements'])}")
    if v.get("pay", {}).get("known"):
        facts.append(f"Оплата: {clean(v['pay']['text'])}")
    if facts:
        parts.append("<ul>" + "".join(f"<li>{xml_escape(f)}</li>" for f in facts) + "</ul>")
    return "".join(parts)


def job_posting_jsonld(v: dict[str, Any], site_url: str) -> dict[str, Any]:
    """JobPosting по требованиям Google: title, description, datePosted, validThrough, hiringOrganization,
    jobLocation обязательно; baseSalary, employmentType, directApply, identifier по возможности."""
    posted = fmt.parse_dt(v.get("published_at") or v.get("created_at"))
    employer = clean(v.get("employer")) or "Работодатель"
    org: dict[str, Any] = {"@type": "Organization", "name": employer}
    if v.get("company_id"):
        org["sameAs"] = f"{site_url}/company/{v['company_id']}"
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": v["profession_title"],
        "description": job_description_html(v),
        "identifier": {"@type": "PropertyValue", "name": SITE_NAME, "value": str(v["id"])},
        "url": f"{site_url}/vacancy/{v['id']}",
        "directApply": bool(v.get("is_direct")),
        "hiringOrganization": org,
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": v.get("city") or "Донецк",
                "addressRegion": "Донецкая Народная Республика",
                "addressCountry": "RU",
            },
        },
    }
    if posted:
        ld["datePosted"] = posted.date().isoformat()
    until = valid_through(v)
    if until:
        ld["validThrough"] = until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if v.get("employment") in EMPLOYMENT_TYPES:
        ld["employmentType"] = EMPLOYMENT_TYPES[v["employment"]]
    if v.get("work_format") == "удалённо":
        ld["jobLocationType"] = "TELECOMMUTE"
        ld["applicantLocationRequirements"] = {"@type": "Country", "name": "RU"}
    if v.get("pay_min") or v.get("pay_max"):
        value: dict[str, Any] = {"@type": "QuantitativeValue", "unitText": UNIT_TEXT.get(v.get("pay_period") or "", "MONTH")}
        if v["pay_min"] and v["pay_max"] and v["pay_min"] != v["pay_max"]:
            value["minValue"], value["maxValue"] = sorted((v["pay_min"], v["pay_max"]))
        else:
            value["value"] = v["pay_min"] or v["pay_max"]
        ld["baseSalary"] = {"@type": "MonetaryAmount", "currency": "RUB", "value": value}
    if v.get("experience") and v.get("no_experience"):
        ld["experienceRequirements"] = {"@type": "OccupationalExperienceRequirements", "monthsOfExperience": 0}
    if v.get("requirements"):
        ld["qualifications"] = v["requirements"]
    return ld


# ------------------------------------------------------------------ тексты страниц-посадок

def _money_range(lo: int, hi: int) -> str:
    if lo == hi:
        return f"{fmt.money(lo)} ₽"
    return f"от {fmt.money(lo)} до {fmt.money(hi)} ₽"


def listing_intro(lead: str, facts: dict[str, Any], places: bool = False) -> list[str]:
    """Вводный текст страницы-посадки из реальных данных выдачи.

    lead: первая фраза с числом вакансий; facts: ответ queries.listing_facts; places: добавить районы.
    Цифры берутся только из базы, ничего не выдумывается: нет данных об оплате, так и пишем.
    """
    parts = [lead]
    pays = facts.get("pay") or []
    if pays:
        bits = []
        for row in pays[:2]:
            period = fmt.PERIOD_LABELS.get(row["period"], row["period"])
            n = fmt.count_label(row["n"], "вакансия", "вакансии", "вакансий")
            bits.append(f"{_money_range(row['lo'], row['hi'])} {period} ({n})")
        parts.append("Оплата, где она указана: " + "; ".join(bits) + ".")
    else:
        parts.append("Оплата в этих вакансиях не указана, её нужно уточнять по телефону.")
    top_places = [(d, n) for d, n in (facts.get("districts") or [])[:3] if n >= 2]  # "больше всего" только при перевесе
    if places and top_places:
        top = ", ".join(f"{fmt.district_label(d)} ({n})" for d, n in top_places)
        parts.append(f"Больше всего вакансий в районах: {top}.")
    extras = []
    if facts.get("no_experience"):
        extras.append(f"без опыта: {facts['no_experience']}")
    if facts.get("daily"):
        extras.append(f"с выплатой каждый день: {facts['daily']}")
    if facts.get("direct"):
        extras.append(f"от работодателей на сайте: {facts['direct']}")
    if extras:
        parts.append("Из них " + ", ".join(extras) + ".")
    return parts


def sentence(text: str) -> str:
    """Фраза с точкой в конце, если там нет другого знака."""
    text = clean(text)
    return text if not text or text[-1] in ".!?…" else text + "."


def listing_description(heading: str, total: int, facts: dict[str, Any], places: bool = False) -> str:
    text = f"{heading}: {fmt.count_label(total, 'вакансия', 'вакансии', 'вакансий')} в Донецке и ДНР."
    pays = facts.get("pay") or []
    if pays:
        row = pays[0]
        text += f" Оплата {_money_range(row['lo'], row['hi'])} {fmt.PERIOD_LABELS.get(row['period'], row['period'])}."
    districts = [fmt.district_label(d) for d, n in (facts.get("districts") or [])[:2] if n >= 2] if places else []
    if districts:
        text += " Чаще всего: " + ", ".join(districts) + "."
    return text


# ------------------------------------------------------------------ карта сайта и robots.txt

def chunks(items: Sequence[Any], size: int | None = None) -> list[Sequence[Any]]:
    size = size or SITEMAP_CHUNK
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def sitemap_urlset(site_url: str, urls: Iterable[tuple[str, str]]) -> str:
    """urls: (путь, дата lastmod)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, lastmod in urls:
        parts.append(f"<url><loc>{xml_escape(path_url(site_url, path))}</loc><lastmod>{lastmod}</lastmod></url>")
    parts.append("</urlset>")
    return "\n".join(parts)


def sitemap_index(site_url: str, files: Iterable[tuple[str, str]]) -> str:
    """files: (имя файла вида /sitemap-vacancies-1.xml, дата lastmod)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, lastmod in files:
        parts.append(f"<sitemap><loc>{xml_escape(site_url + path)}</loc><lastmod>{lastmod}</lastmod></sitemap>")
    parts.append("</sitemapindex>")
    return "\n".join(parts)


def date_of(value: str | None, fallback: datetime) -> str:
    return (fmt.parse_dt(value) or fallback).date().isoformat()


def robots_txt(site_url: str) -> str:
    """robots.txt: служебные пути закрыты, фильтры не закрываем от обхода (их закрывает noindex в странице)."""
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /logout",
        "Disallow: /verify",
        "Disallow: /forgot",
        "Disallow: /reset",
        "Disallow: /account",
        "Disallow: /cabinet",
        "Disallow: /email",
        "Disallow: /admin",
        "Disallow: /razmestit/spasibo",
        "Disallow: /vacancy/*/complaint",
        "Disallow: /vacancy/*/apply",
        "",
        "# Яндекс: параметры сортировки и подстановок не создают новых страниц",
        "Clean-param: sort&pay_from&where /vacancies",
        "",
        f"Host: {site_url}",
        f"Sitemap: {site_url}/sitemap.xml",
    ]
    return "\n".join(lines) + "\n"


def etag_of(body: bytes) -> str:
    return 'W/"' + hashlib.sha1(body).hexdigest()[:20] + '"'


# ------------------------------------------------------------------ заголовки ответов

HTML_CACHE = "public, max-age=0, must-revalidate"


def install_headers(app: Any) -> None:
    """Подключает заголовки поисковой выдачи.

    1. Служебные разделы получают X-Robots-Tag: noindex даже на перенаправлениях и в ответах без HTML.
    2. Публичные страницы гостей, открытые для индекса, получают ETag и Cache-Control с повторной проверкой:
       робот присылает If-None-Match и получает 304 без тела. Вошедшим пользователям и закрытым страницам
       кэш не нужен, им заголовок остаётся прежним (no-store ставит модуль аккаунтов).
    3. robots.txt и карта сайта кэшируются на час.
    """
    from starlette.requests import Request
    from starlette.responses import Response

    @app.middleware("http")
    async def seo_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        path = request.url.path
        if is_private_path(path):
            response.headers.setdefault("X-Robots-Tag", "noindex, follow")
        if request.method != "GET" or response.status_code != 200 or "etag" in response.headers:
            return response
        ctype = response.headers.get("content-type", "")
        is_feed = path == "/robots.txt" or path.startswith("/sitemap")
        is_page = ctype.startswith("text/html") and not getattr(request.state, "user", None) \
            and "x-robots-tag" not in response.headers and "no-store" not in response.headers.get("cache-control", "")
        if not (is_feed or is_page):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        etag = etag_of(body)
        cache = f"public, max-age={SITEMAP_CACHE_SECONDS}" if is_feed else HTML_CACHE
        headers = [(k, v) for k, v in response.raw_headers if k.lower() not in (b"content-length", b"etag", b"cache-control")]
        if request.headers.get("if-none-match") == etag:
            keep = [(k, v) for k, v in headers if k.lower() in (b"set-cookie", b"vary")]
            fresh = Response(status_code=304)
            fresh.raw_headers = [*keep, (b"etag", etag.encode()), (b"cache-control", cache.encode())]
            return fresh
        fresh = Response(content=body, status_code=200)
        fresh.raw_headers = [
            *headers, (b"content-length", str(len(body)).encode()), (b"etag", etag.encode()),
            (b"cache-control", cache.encode()),
        ]
        if is_page:
            fresh.raw_headers.append((b"vary", b"Cookie"))
        return fresh
