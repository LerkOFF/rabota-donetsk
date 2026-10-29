"""SQLite: локальная база. Схема совместима с переносом на PostgreSQL."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from rabota.config import DB_PATH
from rabota.models import RawPost, Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
  url TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', published_at TEXT,
  image_urls TEXT NOT NULL DEFAULT '[]', extra TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS vacancies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profession TEXT NOT NULL, title_raw TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'другое', work_format TEXT, employment TEXT, schedule TEXT,
  pay_min INTEGER, pay_max INTEGER, pay_period TEXT, pay_daily INTEGER NOT NULL DEFAULT 0,
  pay_monthly_estimate INTEGER, city TEXT, district TEXT, experience TEXT, age_min INTEGER,
  requirements TEXT, employer TEXT, contacts TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '', published_at TEXT, kind TEXT NOT NULL DEFAULT 'vacancy',
  dedupe_key TEXT NOT NULL DEFAULT '', duplicate_of INTEGER, status TEXT NOT NULL DEFAULT 'active',
  suspicious INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vac_dedupe ON vacancies(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_vac_pub ON vacancies(published_at);
CREATE INDEX IF NOT EXISTS idx_vac_cat ON vacancies(category);
CREATE TABLE IF NOT EXISTS complaints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id INTEGER NOT NULL, reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_VAC_COLS = [
    "profession", "title_raw", "category", "work_format", "employment", "schedule", "pay_min", "pay_max",
    "pay_period", "pay_daily", "pay_monthly_estimate", "city", "district", "experience", "age_min",
    "requirements", "employer", "contacts", "summary", "source_type", "source", "source_url", "external_id",
    "published_at", "kind", "dedupe_key", "duplicate_of", "status", "suspicious",
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_raw(conn: sqlite3.Connection, post: RawPost) -> bool:
    """Сохранить сырой пост. True, если он новый."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_posts(source_type,source,external_id,url,text,published_at,image_urls,extra)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (post.source_type, post.source, post.external_id, post.url, post.text, post.published_at,
         json.dumps(post.image_urls, ensure_ascii=False), json.dumps(post.extra, ensure_ascii=False)),
    )
    conn.commit()
    return cur.rowcount > 0


def load_raw(conn: sqlite3.Connection, source_type: str | None = None, limit: int | None = None) -> list[RawPost]:
    q, args = "SELECT * FROM raw_posts", []
    if source_type:
        q += " WHERE source_type=?"
        args.append(source_type)
    q += " ORDER BY id"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return [
        RawPost(r["source_type"], r["source"], r["external_id"], r["url"], r["text"], r["published_at"],
                json.loads(r["image_urls"]), json.loads(r["extra"]))
        for r in conn.execute(q, args)
    ]


def save_vacancy(conn: sqlite3.Connection, v: Vacancy) -> int:
    d = v.to_dict()
    d["contacts"] = json.dumps(d["contacts"], ensure_ascii=False)
    d["pay_daily"] = int(d["pay_daily"])
    d["suspicious"] = int(d["suspicious"])
    cur = conn.execute(
        f"INSERT INTO vacancies({','.join(_VAC_COLS)}) VALUES ({','.join('?' * len(_VAC_COLS))})",
        [d[c] for c in _VAC_COLS],
    )
    conn.commit()
    return int(cur.lastrowid)


def vacancy_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["contacts"] = json.loads(d["contacts"] or "{}")
    d["pay_daily"] = bool(d["pay_daily"])
    d["suspicious"] = bool(d["suspicious"])
    return d
