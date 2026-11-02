"""Индексация страниц работодателей и карта сайта.

Правило:
- заблокированные и слитые карточки не индексируются никогда;
- автокарточка без подтверждения индексируется, только если у неё не меньше N вакансий с учётом архива
  (N - настройка `employer_index_min`, по умолчанию 3), иначе `noindex, follow`;
- подтверждённая карточка (`verified_at`) индексируется, если у неё есть хоть одна вакансия, открытая или в архиве;
- карточка, которую завёл сам работодатель, - если у неё есть хотя бы одна открытая вакансия.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from rabota.admin import settings
from rabota.employers_auto.archive import ARCHIVE_SQL
from rabota.web import queries as q

DEFAULT_MIN = 3


def min_vacancies(conn: sqlite3.Connection | None) -> int:
    try:
        return int(settings.effective(conn, "employer_index_min"))
    except Exception:  # noqa: BLE001 - сбой чтения настроек не должен ронять страницу
        return DEFAULT_MIN


def is_indexable(company: dict[str, Any] | None, visible: int, archived: int, minimum: int = DEFAULT_MIN) -> bool:
    if not company or company.get("blocked_at") or company.get("merged_into"):
        return False
    if company.get("verified_at"):
        return visible + archived >= 1
    if company.get("origin") == "auto":
        return visible + archived >= minimum
    return visible >= 1


def sitemap_companies(conn: sqlite3.Connection) -> list[tuple[int, str | None]]:
    """(id, дата последней публикации) для карты сайта: только индексируемые карточки."""
    minimum = min_vacancies(conn)
    rows = conn.execute(
        f"SELECT c.*, a.vis AS _vis, a.arch AS _arch, a.ts AS _ts FROM companies c JOIN ("
        f"SELECT company_id, SUM(CASE WHEN {q.VISIBLE_SQL} THEN 1 ELSE 0 END) AS vis, "
        f"SUM(CASE WHEN {ARCHIVE_SQL} THEN 1 ELSE 0 END) AS arch, "
        f"MAX(CASE WHEN {q.VISIBLE_SQL} OR {ARCHIVE_SQL} THEN COALESCE(published_at, created_at) END) AS ts "
        f"FROM vacancies WHERE company_id IS NOT NULL GROUP BY company_id) a ON a.company_id = c.id ORDER BY c.id"
    ).fetchall()
    out: list[tuple[int, str | None]] = []
    for r in rows:
        d = dict(r)
        if is_indexable(d, int(d["_vis"] or 0), int(d["_arch"] or 0), minimum):
            out.append((int(d["id"]), d["_ts"]))
    return out
