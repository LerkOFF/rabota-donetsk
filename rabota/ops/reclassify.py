"""Пересчёт должности, отрасли и формата у уже сохранённых вакансий после улучшения разбора.

    python -m rabota.ops.reclassify [--source hh] [--dry-run]

Берёт сырой пост каждой вакансии, разбирает его текущими правилами и обновляет у вакансии только
`profession`, `category`, `work_format` и `dedupe_key`. Номер вакансии, избранное, жалобы, модерация,
срок жизни, зарплата и остальные поля не меняются. Не трогает вакансии, которые правил владелец карточки,
и всё, что создано не разбором. Если пост даёт не ровно одну вакансию, пропускает его. Повторный запуск
ничего не меняет.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from rabota.db import connect
from rabota.models import RawPost
from rabota.pipeline.run import analyze

FIELDS = ("profession", "category", "work_format", "dedupe_key")


def _raw(row: sqlite3.Row) -> RawPost:
    return RawPost(
        row["source_type"], row["source"], row["external_id"], row["url"], row["text"], row["published_at"],
        json.loads(row["image_urls"] or "[]"), json.loads(row["extra"] or "{}"),
    )


def reclassify(conn: sqlite3.Connection, *, source: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Обновить поля вакансий. Возвращает счётчики и список изменений (для печати)."""
    query = (
        "SELECT v.id AS vid, v.profession, v.category, v.work_format, v.dedupe_key, r.* FROM vacancies v "
        "JOIN raw_posts r ON r.source_type = v.source_type AND r.source = v.source AND r.external_id = v.external_id "
        "WHERE v.origin = 'parsed' AND v.owner_edited_at IS NULL"
    )
    args: list[Any] = []
    if source:
        query += " AND v.source_type = ?"
        args.append(source)
    stats: dict[str, Any] = {"checked": 0, "changed": 0, "skipped": 0, "items": []}
    for row in conn.execute(query + " ORDER BY v.id", args).fetchall():
        stats["checked"] += 1
        result = analyze(_raw(row))
        if result.kind != "vacancy" or len(result.vacancies) != 1:
            stats["skipped"] += 1
            continue
        new = result.vacancies[0]
        fresh = {"profession": new.profession, "category": new.category, "work_format": new.work_format, "dedupe_key": new.dedupe_key}
        diff = {k: (row[k], v) for k, v in fresh.items() if row[k] != v and k != "dedupe_key"}
        if not diff:
            continue
        stats["changed"] += 1
        stats["items"].append({"id": row["vid"], "diff": diff})
        if not dry_run:
            conn.execute(
                "UPDATE vacancies SET profession = ?, category = ?, work_format = ?, dedupe_key = ? WHERE id = ?",
                (*(fresh[k] for k in FIELDS), row["vid"]),
            )
    if not dry_run:
        conn.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=None, help="только этот источник: hh, telegram, ...")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    conn = connect()
    try:
        res = reclassify(conn, source=args.source, dry_run=args.dry_run)
    finally:
        conn.close()
    for item in res["items"]:
        print(f"  {item['id']}: " + "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in item["diff"].items()))
    print(f"проверено {res['checked']}, изменено {res['changed']}, пропущено {res['skipped']}" + (" (пробный запуск)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
