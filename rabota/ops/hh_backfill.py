"""Разовая добавка: полные требования для вакансий hh.ru, собранных по обрезанным фрагментам выдачи.

    python -m rabota.ops.hh_backfill [--limit 400] [--dry-run]

Для каждой видимой вакансии hh запрашивает её страницу (с паузой), заново разбирает и обновляет
только поле requirements, если оно стало длиннее. Остальные поля вакансии не трогает.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import httpx

from rabota import config
from rabota.collectors import hh
from rabota.db import connect
from rabota.models import RawPost
from rabota.pipeline.run import process

log = logging.getLogger("rabota.hh_backfill")


def new_requirements(client: httpx.Client, vacancy_id: str, extra: dict) -> tuple[str | None, str | None]:
    """(требования, полный текст поста) по странице вакансии; (None, None) при сбое."""
    description = hh.fetch_description(client, vacancy_id)
    if not description:
        return None, None
    raw: RawPost = hh.to_raw_post({**extra, "vacancyId": vacancy_id}, description)
    found = process(raw)
    return (found[0].requirements if found else None), raw.text


def run(*, limit: int = 400, dry_run: bool = False, client: httpx.Client | None = None, pause: float = hh.PAUSE_SECONDS,
        db_path=None) -> int:
    conn = connect(db_path)
    own = client is None
    client = client or httpx.Client(timeout=hh.TIMEOUT, follow_redirects=True, headers={"User-Agent": config.USER_AGENT})
    updated = 0
    try:
        rows = conn.execute(
            "SELECT v.id, v.requirements, r.external_id, r.extra FROM vacancies v "
            "JOIN raw_posts r ON r.source_type = 'hh' AND r.url = v.source_url "
            "WHERE v.source_type = 'hh' AND v.status = 'active' AND v.duplicate_of IS NULL ORDER BY v.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            time.sleep(pause)
            req, text = new_requirements(client, row["external_id"], json.loads(row["extra"] or "{}"))
            if not req or len(req) <= len(row["requirements"] or ""):
                continue
            updated += 1
            if dry_run:
                print(row["id"], req)
                continue
            conn.execute("UPDATE vacancies SET requirements = ? WHERE id = ?", (req, row["id"]))
            conn.execute("UPDATE raw_posts SET text = ? WHERE source_type = 'hh' AND external_id = ?", (text, row["external_id"]))
            conn.commit()
    finally:
        if own:
            client.close()
        conn.close()
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    print(f"обновлено вакансий: {run(limit=args.limit, dry_run=args.dry_run)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
