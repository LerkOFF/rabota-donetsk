"""Проход по активным вакансиям: мусорные (название не проходит фильтр pipeline.quality) уходят на проверку.

    python -m rabota quality-sweep [--dry-run]

Мусорная вакансия получает `suspicious=1`, `status='hidden'`, `moderation_status='pending'` и причину в `moderation_note`:
из выдачи и sitemap она пропадает, в админке (разделы «Вакансии» и «Модерация») видна с причиной.
Команда идемпотентна: скрытые уже не активны и повторно не трогаются. Вакансии, которые администратор
одобрил вручную, не трогаются.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from rabota.admin import audit
from rabota.pipeline.quality import note_for, profession_problem

ACTION = "vacancy.quality_hide"

ACTIVE_SQL = (
    "SELECT id, profession FROM vacancies WHERE kind = 'vacancy' AND status = 'active' AND moderation_status = 'approved'"
    " AND suspicious = 0 AND duplicate_of IS NULL"
    " AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action = 'vacancy.approve' AND a.target_id = CAST(vacancies.id AS TEXT))"
    " ORDER BY id"
)


def find_junk(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """(id, должность, причина) для активных вакансий с мусорным названием."""
    out = []
    for r in conn.execute(ACTIVE_SQL):
        problem = profession_problem(r["profession"])
        if problem:
            out.append((int(r["id"]), r["profession"], problem))
    return out


def sweep(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """Найти мусорные активные вакансии и скрыть их. dry_run только показывает, что было бы скрыто."""
    checked = int(conn.execute(ACTIVE_SQL.replace("SELECT id, profession", "SELECT COUNT(*) AS n", 1).split(" ORDER BY")[0]).fetchone()[0])
    junk = find_junk(conn)
    if not dry_run:
        for vid, profession, problem in junk:
            conn.execute(
                "UPDATE vacancies SET suspicious = 1, status = 'hidden', moderation_status = 'pending', moderation_note = ?"
                " WHERE id = ? AND status = 'active'",
                (note_for(problem, profession), vid),
            )
            conn.commit()
            audit.log(conn, None, ACTION, "vacancy", vid, {"profession": profession[:60], "reason": problem})
    return {
        "checked": checked,
        "junk": len(junk),
        "hidden": 0 if dry_run else len(junk),
        "dry_run": dry_run,
        "items": [{"id": vid, "profession": p, "reason": reason} for vid, p, reason in junk],
    }
