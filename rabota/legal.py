"""Юридические документы сайта: загрузка из docs/legal, подстановка данных оператора, отрисовка Markdown.

Тексты лежат в `docs/legal/*.md`. Данные оператора берутся из окружения (в репозитории их нет):
OPERATOR_NAME, OPERATOR_STATUS, OPERATOR_INN, OPERATOR_EMAIL, OPERATOR_ADDRESS, SITE_URL, SITE_NAME.
Если переменная не задана, на странице видна заглушка "[укажите ...]", а в лог пишется предупреждение.

Отрисовщик Markdown маленький и безопасный: заголовки, абзацы, списки, цитаты (пометка "Черновик"),
жирный текст и ссылки. Весь HTML из текста экранируется, значения переменных в разметку не попадают.
"""
from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from rabota import config

log = logging.getLogger(__name__)

LEGAL_DIR = config.ROOT / "docs" / "legal"
DEFAULT_SITE_URL = "https://работа-донецк.рф"
DEFAULT_SITE_NAME = "Работа Донецк"
# Дата редакции текстов. Менять при каждой правке документов, на боевом сервере можно задать LEGAL_UPDATED_AT.
UPDATED_AT = "29.10.2026"


@dataclass(frozen=True)
class Document:
    slug: str
    filename: str
    label: str
    summary: str


DOCUMENTS: tuple[Document, ...] = (
    Document("privacy", "privacy.md", "Конфиденциальность", "Какие данные мы собираем, зачем, как долго храним и как вы можете ими управлять."),
    Document("terms", "terms.md", "Соглашение", "Правила пользования сайтом для соискателей и работодателей, что запрещено, модерация и блокировка."),
    Document("employers", "employers.md", "Для работодателей", "Как составить вакансию, пройти модерацию, продлить её и писать соискателям."),
    Document("consent", "consent.md", "Согласие на обработку", "Согласие на обработку персональных данных при регистрации."),
    Document("dissemination", "dissemination.md", "Показ резюме", "Необязательное согласие на показ резюме другим лицам."),
    Document("cookies", "cookies.md", "Cookie", "Какие файлы cookie использует сайт и как ими управлять."),
    Document("requests", "requests.md", "Обращения и удаление", "Как подать обращение, жалобу или запрос на удаление данных и в какие сроки мы отвечаем."),
    Document("sources", "sources.md", "Открытые источники", "Откуда берутся вакансии из открытых источников и как удалить свою вакансию."),
)
_BY_SLUG = {d.slug: d for d in DOCUMENTS}


class UnknownDocument(KeyError):
    """Нет документа с таким именем."""


# --------------------------------------------------------------------------- переменные оператора

# имя переменной в тексте: (переменная окружения, заглушка)
_OPERATOR_FIELDS: dict[str, tuple[str, str]] = {
    "operator_name": ("OPERATOR_NAME", "[укажите ФИО или название оператора]"),
    "operator_status": ("OPERATOR_STATUS", "[укажите статус оператора]"),
    "operator_inn": ("OPERATOR_INN", "[укажите ИНН оператора]"),
    "operator_email": ("OPERATOR_EMAIL", "[укажите почту для обращений]"),
    "operator_address": ("OPERATOR_ADDRESS", "[укажите адрес оператора]"),
}
_warned: set[str] = set()


def reset_warnings() -> None:
    """Сбросить память о выданных предупреждениях (нужно в тестах)."""
    _warned.clear()


def variables(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Значения переменных для текстов. Не заданные данные оператора заменяются заглушками и попадают в лог."""
    source = os.environ if env is None else env

    def get(name: str) -> str:
        return (source.get(name) or "").strip()

    out: dict[str, str] = {}
    for key, (env_name, placeholder) in _OPERATOR_FIELDS.items():
        value = get(env_name)
        if not value:
            value = placeholder
            if env_name not in _warned:
                _warned.add(env_name)
                log.warning("Не задана переменная %s: в юридических текстах показана заглушка", env_name)
        out[key] = value
    out["site_url"] = (get("SITE_URL") or DEFAULT_SITE_URL).rstrip("/")
    out["site_name"] = get("SITE_NAME") or DEFAULT_SITE_NAME
    out["updated_at"] = get("LEGAL_UPDATED_AT") or UPDATED_AT
    return out


def missing_settings(env: Mapping[str, str] | None = None) -> list[str]:
    """Какие переменные окружения с данными оператора не заданы (для проверки перед запуском)."""
    source = os.environ if env is None else env
    return [name for name, _ in _OPERATOR_FIELDS.values() if not (source.get(name) or "").strip()]


# --------------------------------------------------------------------------- отрисовка Markdown

_VAR = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
_TOKEN_CHARS = ""
_TOKEN = re.compile("(\\d+)")
_SAFE_URL = re.compile(r"^(?:https?://[^/\s]|mailto:|/(?!/)|#)", re.IGNORECASE)


@dataclass
class Rendered:
    """Готовая страница: заголовок документа, HTML тела и оглавление (id раздела, название)."""

    title: str = ""
    html: str = ""
    toc: list[tuple[str, str]] = field(default_factory=list)


def _inline(text: str, values: Mapping[str, str]) -> str:
    """Строчная разметка. Сначала переменные прячутся в метки, потом экранируется остальное."""
    for ch in _TOKEN_CHARS:
        text = text.replace(ch, "")
    stash: list[str] = []

    def hide(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            return m.group(0)
        stash.append(html.escape(values[name], quote=True))
        return f"{len(stash) - 1}"

    text = html.escape(_VAR.sub(hide, text), quote=True)

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        if not _SAFE_URL.match(url):
            return label
        rel = ' rel="noopener noreferrer"' if url.lower().startswith("http") else ""
        return f'<a href="{url}"{rel}>{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _TOKEN.sub(lambda m: stash[int(m.group(1))], text)


def render_markdown(text: str, values: Mapping[str, str] | None = None, *, id_prefix: str = "s") -> Rendered:
    """Markdown в безопасный HTML. Первый заголовок первого уровня становится названием документа."""
    values = values if values is not None else {}
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    title = ""
    para: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    list_tag = ""

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para), values)}</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            out.append(f'<p class="notice notice--warn"><span>{_inline(" ".join(quote), values)}</span></p>')
            quote.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            cls = "about__list" + (" legal__ol" if list_tag == "ol" else "")
            body = "".join(f"<li>{_inline(i, values)}</li>" for i in items)
            out.append(f'<{list_tag} class="{cls}">{body}</{list_tag}>')
            items.clear()
        list_tag = ""

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_list()

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_all()
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_all()
            level, label = len(heading.group(1)), heading.group(2)
            if level == 1:
                if not title:
                    title = _inline(label, values)
                continue
            heading_id = f"{id_prefix}{len(toc) + 1}" if level == 2 else ""
            attrs = f' id="{heading_id}"' if heading_id else ""
            if level == 2:
                toc.append((heading_id, _inline(label, values)))
                out.append(f'<h2 class="h3"{attrs}>{_inline(label, values)}</h2>')
            else:
                out.append(f"<h3>{_inline(label, values)}</h3>")
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
    return Rendered(title=title, html="\n".join(out), toc=toc)


# --------------------------------------------------------------------------- документы


def list_documents() -> tuple[Document, ...]:
    return DOCUMENTS


def get_document(slug: str) -> Document | None:
    return _BY_SLUG.get(slug)


@cache
def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def source_text(slug: str, directory: Path | None = None) -> str:
    """Исходный Markdown документа."""
    doc = _BY_SLUG.get(slug)
    if doc is None:
        raise UnknownDocument(slug)
    return _source((directory or LEGAL_DIR) / doc.filename)


def document_title(slug: str) -> str:
    """Название документа без подстановки: первая строка с одним решётчатым знаком."""
    for line in source_text(slug).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug


def render_document(slug: str, values: Mapping[str, str] | None = None) -> Rendered:
    """Отрисовать документ с подстановкой данных оператора из окружения (или переданных значений)."""
    return render_markdown(source_text(slug), values if values is not None else variables())


def consent_text(values: Mapping[str, str] | None = None) -> str:
    """HTML согласия на обработку персональных данных для окна при регистрации (без заголовка страницы)."""
    return render_markdown(source_text("consent"), values if values is not None else variables(), id_prefix="consent-").html
