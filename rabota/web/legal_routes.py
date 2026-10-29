"""Маршруты юридических документов: /legal и /legal/<документ>.

Подключение в приложении: `from rabota.web import legal_routes` и `legal_routes.install(app)`.
Модуль не импортирует rabota.web.app, поэтому подключать его можно из create_app без круговых импортов.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from rabota import legal
from rabota.web import seo as seolib

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version(name: str) -> str:
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


def build_router(site_url: str | None = None) -> APIRouter:
    """Роутер с маршрутами /legal и /legal/{slug}. site_url нужен для canonical и подписей в шаблоне."""
    router = APIRouter()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    base_url = (site_url or legal.variables()["site_url"]).rstrip("/")
    templates.env.globals.update(
        asset=lambda name: f"/static/{name}?v={_asset_version(name)}",
        site_url=base_url,
    )

    def render(request: Request, context: dict[str, Any], seo: dict[str, Any]) -> Response:
        ctx = {"path": request.url.path, "seo": seolib.finalize(seo, base_url, request.url.path), **context}
        response = templates.TemplateResponse(request, "legal/page.html", ctx)
        seolib.apply_headers(response, ctx["seo"])
        return response

    @router.get("/legal")
    def legal_index(request: Request) -> Response:
        docs = [
            {"slug": d.slug, "title": legal.document_title(d.slug), "summary": d.summary}
            for d in legal.list_documents()
        ]
        return render(
            request,
            {"documents": docs, "doc": None, "title": "Правила и документы"},
            {
                "title": "Правила и документы",
                "description": "Политика конфиденциальности, пользовательское соглашение, правила для работодателей, "
                               "согласие на обработку данных и другие документы сайта.",
                "breadcrumbs": [("Главная", "/"), ("Документы", "/legal")],
            },
        )

    @router.get("/legal/{slug}")
    def legal_document(request: Request, slug: str) -> Response:
        document = legal.get_document(slug)
        if document is None:
            raise HTTPException(status_code=404)
        values = legal.variables()
        page = legal.render_document(slug, values)
        docs = [{"slug": d.slug, "title": d.label} for d in legal.list_documents()]
        title = legal.document_title(slug)
        return render(
            request,
            {
                "documents": docs,
                "doc": {"slug": document.slug, "summary": document.summary},
                "title": title,
                "body": page.html,
                "toc": page.toc,
            },
            {
                "title": title,
                "description": f"{title}. {document.summary}",
                "breadcrumbs": [("Главная", "/"), ("Документы", "/legal"), (title, f"/legal/{slug}")],
            },
        )

    return router


def install(app: FastAPI, site_url: str | None = None) -> None:
    """Добавить маршруты юридических документов в приложение."""
    app.include_router(build_router(site_url))
