"""Данные для страницы работодателя /company/{id}, которые добавляют автокарточки: архив, индексация, честные поля."""
from __future__ import annotations

import sqlite3
from typing import Any

from rabota.employers_auto import cards, seo


def company_extra(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    """Показатели карточки: вакансии открытые и в архиве, нужно ли индексировать, автокарточка ли без владельца."""
    counts = cards.counts(conn, int(row["id"]))
    minimum = seo.min_vacancies(conn)
    return {
        "counts": counts,
        "archive_count": counts["archived"],
        "archive_url": f"/company/{row['id']}/archive",
        "auto_card": cards.is_auto_unclaimed(row),
        "indexable": seo.is_indexable(row, counts["visible"], counts["archived"], minimum),
        "index_min": minimum,
    }


def apply_to_company(company: dict[str, Any], row: dict[str, Any] | None, extra: dict[str, Any]) -> None:
    """Дополнить словарь company для шаблона. У автокарточки показываем только то, что есть в источнике:
    без города по умолчанию и без придуманных сведений."""
    company["archive_count"] = extra.get("archive_count", 0)
    company["archive_url"] = extra.get("archive_url", "")
    company["auto_card"] = bool(extra.get("auto_card"))
    if row and row.get("origin") == "auto":
        company["city"] = (row.get("city") or "").strip()
        company["legal_form"] = row.get("legal_form") or ""


def clean_jsonld(ld: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """Organization без выдуманных данных: адрес только если город известен."""
    if row and row.get("origin") == "auto" and not (row.get("city") or "").strip():
        ld.pop("address", None)
    return ld
