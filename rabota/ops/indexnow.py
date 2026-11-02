"""Мгновенное уведомление Яндекса и других поисковиков о новых вакансиях (протокол IndexNow).

    python -m rabota.ops.indexnow [--dry-run]

Запускается таймером раз в 10 минут. Берёт вакансии, которые видны на сайте и появились после прошлого запуска
(номер последней отправленной хранится в файле состояния рядом с базой), и отправляет их адреса пакетом.
Без INDEXNOW_KEY молча ничего не делает. Ключ: 8-128 символов из латиницы, цифр и дефиса; тот же ключ
сайт отдаёт по адресу /<ключ>.txt, так поисковик убеждается, что адреса отправил владелец.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from rabota import config
from rabota.web import queries

log = logging.getLogger("rabota.indexnow")

ENDPOINT = "https://yandex.com/indexnow"
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
MAX_URLS = 500  # за один запуск; остальное уйдёт со следующим
STATIC_URLS = ("/", "/vacancies")


def key() -> str:
    value = config.get("INDEXNOW_KEY").strip()
    return value if KEY_RE.match(value) else ""


def load_last_id(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("last_id", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def save_last_id(path: Path, last_id: int) -> None:
    path.write_text(json.dumps({"last_id": last_id}), encoding="utf-8")


def new_vacancy_ids(conn, last_id: int, limit: int = MAX_URLS) -> list[int]:
    rows = conn.execute(
        f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL} AND id > ? ORDER BY id LIMIT ?", (last_id, limit)
    ).fetchall()
    return [int(r[0]) for r in rows]


def build_payload(site: str, ik: str, ids: list[int]) -> dict[str, Any]:
    host = site.split("://", 1)[-1].split("/", 1)[0]
    urls = [f"{site}{p}" for p in STATIC_URLS] + [f"{site}/vacancy/{i}" for i in ids]
    return {"host": host, "key": ik, "keyLocation": f"{site}/{ik}.txt", "urlList": urls}


def submit(payload: dict[str, Any], client: httpx.Client | None = None) -> bool:
    own = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": config.USER_AGENT})
    try:
        resp = client.post(ENDPOINT, json=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        ok = resp.status_code in (200, 202)
        if not ok:
            log.warning("IndexNow: ответ %s: %s", resp.status_code, resp.text[:200])
        return ok
    except httpx.HTTPError as exc:
        log.warning("IndexNow: запрос не удался: %s", exc)
        return False
    finally:
        if own:
            client.close()


def run(*, dry_run: bool = False, client: httpx.Client | None = None) -> int:
    ik = key()
    if not ik:
        return 0
    state = Path(config.get("INDEXNOW_STATE_PATH") or config.DB_PATH.parent / "indexnow-state.json")
    conn = queries.open_conn()
    try:
        ids = new_vacancy_ids(conn, load_last_id(state))
    finally:
        conn.close()
    if not ids:
        return 0
    payload = build_payload(config.site_url(), ik, ids)
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return len(ids)
    if submit(payload, client):
        save_last_id(state, ids[-1])
        return len(ids)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    sent = run(dry_run=args.dry_run)
    print(f"IndexNow: отправлено вакансий {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
