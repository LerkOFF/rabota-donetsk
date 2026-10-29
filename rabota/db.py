"""SQLite: локальная база. Схема совместима с переносом на PostgreSQL."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rabota.config import DB_PATH
from rabota.lifecycle import is_stale, to_utc_iso
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
  external_id TEXT NOT NULL DEFAULT '', published_at TEXT, last_seen_at TEXT, kind TEXT NOT NULL DEFAULT 'vacancy',
  dedupe_key TEXT NOT NULL DEFAULT '', duplicate_of INTEGER, status TEXT NOT NULL DEFAULT 'active',
  suspicious INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vac_dedupe ON vacancies(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_vac_pub ON vacancies(published_at);
CREATE INDEX IF NOT EXISTS idx_vac_cat ON vacancies(category);
-- Аккаунты. Роли: job_seeker (ищет работу), employer (работодатель), admin.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('job_seeker','employer','admin')),
  name TEXT NOT NULL DEFAULT '', phone TEXT,
  email_verified_at TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked')),
  consent_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS email_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL CHECK (purpose IN ('verify','reset','change')),
  new_email TEXT, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, ip TEXT, user_agent TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', site TEXT, city TEXT, logo_path TEXT,
  verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '', profession TEXT, city TEXT, district TEXT,
  pay_min INTEGER, pay_period TEXT, work_formats TEXT NOT NULL DEFAULT '[]', about TEXT NOT NULL DEFAULT '',
  experience TEXT NOT NULL DEFAULT '[]', skills TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'hidden' CHECK (visibility IN ('hidden','public','open')),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, cover_letter TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','viewed','invited','rejected')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(vacancy_id, user_id)
);
CREATE TABLE IF NOT EXISTS favorites (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, vacancy_id)
);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, employer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  seeker_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  vacancy_id INTEGER REFERENCES vacancies(id) ON DELETE SET NULL, application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
  started_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_message_at TEXT,
  UNIQUE(employer_user_id, seeker_user_id, vacancy_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS user_blocks (
  blocker_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  blocked_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (blocker_user_id, blocked_user_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('vacancy','user','message','resume')), target_id INTEGER NOT NULL,
  reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS complaints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id INTEGER NOT NULL, reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Состояние приёма данных в реальном времени: на источник последний id, время успеха и ошибка.
CREATE TABLE IF NOT EXISTS ingest_state (
  source TEXT PRIMARY KEY, last_id INTEGER, updated_at TEXT, last_ok_at TEXT, last_error TEXT
);
"""

_VAC_COLS = [
    "profession", "title_raw", "category", "work_format", "employment", "schedule", "pay_min", "pay_max",
    "pay_period", "pay_daily", "pay_monthly_estimate", "city", "district", "experience", "age_min",
    "requirements", "employer", "contacts", "summary", "source_type", "source", "source_url", "external_id",
    "published_at", "last_seen_at", "kind", "dedupe_key", "duplicate_of", "status", "suspicious",
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавить колонки, появившиеся позже, в уже существующую базу."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(vacancies)")}
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN last_seen_at TEXT")
        conn.execute("UPDATE vacancies SET last_seen_at = COALESCE(published_at, created_at)")
    # вакансии работодателей: владелец, компания, происхождение и модерация (у спарсенных одобрено сразу)
    for col, ddl in (
        ("owner_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("company_id", "INTEGER REFERENCES companies(id) ON DELETE SET NULL"),
        ("origin", "TEXT NOT NULL DEFAULT 'parsed'"),
        ("moderation_status", "TEXT NOT NULL DEFAULT 'approved'"),
        ("moderation_note", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} {ddl}")
    conn.commit()
    from rabota.admin.schema import migrate as admin_migrate  # журнал действий, стоп-лист, запросы по ПДн

    admin_migrate(conn)
    from rabota.messaging.schema import migrate as messaging_migrate  # отклики, переписка, уведомления

    messaging_migrate(conn)


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
    # срок жизни идёт от последней публикации; старый пост сразу сохраняется скрытым
    d["last_seen_at"] = to_utc_iso(d["last_seen_at"] or d["published_at"]) or datetime.now(UTC).replace(microsecond=0).isoformat()
    if d["status"] == "active" and is_stale(d["last_seen_at"]):
        d["status"] = "expired"
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
