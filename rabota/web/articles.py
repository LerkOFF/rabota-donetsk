"""Статьи блога: Markdown из docs/articles/*.md, отрисовка на сервере, оглавление, JSON-LD Article.

Файл статьи начинается с блока метаданных между строками `---`:

    ---
    slug: kak-sostavit-rezyume
    title: Как составить резюме
    description: Короткое описание для поиска, 110-160 знаков
    published: 2026-11-02
    updated: 2026-11-02
    topic: Резюме
    related: kak-projti-sobesedovanie, podrabotka-studentu
    search: /vacancies?no_experience=1 | Вакансии без опыта
    ---

Дальше обычный Markdown (заголовки ## и ###, абзацы, списки, жирный, курсив, ссылки, цитата как выноска "Важно").
HTML из текста экранируется. Правила содержания: docs/CONTENT.md.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from rabota import config

log = logging.getLogger(__name__)

ARTICLES_DIR = config.ROOT / "docs" / "articles"
WORDS_PER_MINUTE = 180  # спокойное чтение, для подписи "N мин"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_URL = re.compile(r"^(?:https?://[^/\s]|mailto:|/(?!/)|#)", re.IGNORECASE)
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")


@dataclass
class Article:
    slug: str
    title: str
    description: str
    published: date
    updated: date
    topic: str = ""
    related: tuple[str, ...] = ()
    search_url: str = ""
    search_label: str = ""
    body: str = ""  # исходный Markdown без метаданных
    html: str = ""
    toc: list[tuple[str, str]] = field(default_factory=list)
    words: int = 0

    @property
    def minutes(self) -> int:
        return max(1, round(self.words / WORDS_PER_MINUTE))

    @property
    def path(self) -> str:
        return f"/blog/{self.slug}"


def _inline(text: str) -> str:
    """Строчная разметка: сначала экранирование, потом ссылки, жирный и курсив."""
    text = html.escape(text, quote=True)

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        if not _SAFE_URL.match(url):
            return label
        rel = ' rel="noopener noreferrer"' if url.lower().startswith("http") else ""
        return f'<a href="{html.escape(url, quote=True)}"{rel}>{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _ITALIC.sub(r"<em>\1</em>", text)


def render_markdown(text: str) -> tuple[str, list[tuple[str, str]], int]:
    """Markdown в безопасный HTML, оглавление (id, название) по заголовкам второго уровня и число слов."""
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    para: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    list_tag = ""
    words = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            out.append(f'<aside class="callout" role="note"><p>{_inline(" ".join(quote))}</p></aside>')
            quote.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            cls = "article__ol" if list_tag == "ol" else "article__ul"
            out.append(f'<{list_tag} class="{cls}">' + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{list_tag}>")
            items.clear()
        list_tag = ""

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_list()

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            flush_all()
            continue
        words += len(stripped.split())
        heading = _HEADING.match(stripped)
        if heading:
            flush_all()
            level, label = len(heading.group(1)), heading.group(2)
            if level == 1:
                continue
            if level == 2:
                hid = f"s{len(toc) + 1}"
                toc.append((hid, html.escape(label)))
                out.append(f'<h2 id="{hid}">{_inline(label)}</h2>')
            else:
                out.append(f"<h3>{_inline(label)}</h3>")
            continue
        if stripped.startswith(">"):
            flush_para()
            flush_list()
            quote.append(stripped.lstrip(">").strip())
            continue
        bullet, ordered = _BULLET.match(stripped), _ORDERED.match(stripped)
        item = bullet or ordered
        indented = raw.startswith((" ", "\t"))
        if item and not (indented and items):
            flush_para()
            flush_quote()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append(item.group(1))
            continue
        if items and indented:
            items[-1] += " " + stripped
            continue
        flush_quote()
        flush_list()
        para.append(stripped)
    flush_all()
    return "\n".join(out), toc, words


def parse(source: str, filename: str = "") -> Article:
    """Файл статьи: метаданные и текст. Ошибка в метаданных: ValueError с именем файла."""
    lines = source.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{filename}: нет блока метаданных")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"{filename}: блок метаданных не закрыт") from exc
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    for key in ("slug", "title", "description", "published"):
        if not meta.get(key):
            raise ValueError(f"{filename}: нет поля {key}")
    if not SLUG_RE.match(meta["slug"]):
        raise ValueError(f"{filename}: недопустимый slug {meta['slug']!r}")
    published = date.fromisoformat(meta["published"])
    updated = date.fromisoformat(meta.get("updated") or meta["published"])
    body = "\n".join(lines[end + 1:]).strip()
    html_, toc, words = render_markdown(body)
    search_url, _, search_label = (meta.get("search") or "").partition("|")
    return Article(
        slug=meta["slug"], title=meta["title"], description=meta["description"], published=published, updated=updated,
        topic=meta.get("topic", ""), related=tuple(s.strip() for s in meta.get("related", "").split(",") if s.strip()),
        search_url=search_url.strip(), search_label=search_label.strip(), body=body, html=html_, toc=toc, words=words,
    )


@cache
def _load(directory: str) -> tuple[Article, ...]:
    items: list[Article] = []
    for path in sorted(Path(directory).glob("*.md")):
        try:
            items.append(parse(path.read_text(encoding="utf-8"), path.name))
        except (ValueError, OSError) as exc:
            log.warning("статья %s пропущена: %s", path.name, exc)
    items.sort(key=lambda a: (a.published, a.slug), reverse=True)
    return tuple(items)


def all_articles(directory: Path | str | None = None) -> tuple[Article, ...]:
    """Статьи от новых к старым."""
    return _load(str(directory or ARTICLES_DIR))


def get(slug: str, directory: Path | str | None = None) -> Article | None:
    return next((a for a in all_articles(directory) if a.slug == slug), None)


def related(article: Article, limit: int = 3, directory: Path | str | None = None) -> list[Article]:
    """Читайте также: сначала указанные в метаданных, затем остальные свежие."""
    pool = {a.slug: a for a in all_articles(directory) if a.slug != article.slug}
    chosen = [pool[s] for s in article.related if s in pool]
    chosen += [a for a in pool.values() if a not in chosen]
    return chosen[:limit]


def article_jsonld(a: Article, site_url: str, org_id: str, image: str) -> dict[str, Any]:
    """Article по правилам Google: заголовок, даты, автор и издатель организация."""
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": a.title,
        "description": a.description,
        "datePublished": a.published.isoformat(),
        "dateModified": a.updated.isoformat(),
        "inLanguage": "ru",
        "mainEntityOfPage": f"{site_url}{a.path}",
        "image": image,
        "author": {"@type": "Organization", "name": "Работа Донецк", "url": site_url + "/"},
        "publisher": {"@id": org_id},
    }
