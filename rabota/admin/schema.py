"""Таблицы и колонки служебной части. Вызывается из rabota.db.connect, повторный запуск безопасен."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_user_id INTEGER, actor_email TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL, target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '',
  details TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(id DESC);
CREATE TABLE IF NOT EXISTS stoplist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('phone','nick','employer','keyword')),
  value TEXT NOT NULL, normalized TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, created_by INTEGER,
  hits INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT,
  UNIQUE (kind, normalized)
);
CREATE TABLE IF NOT EXISTS legal_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'web',
  requester_name TEXT NOT NULL DEFAULT '', requester_contact TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','in_progress','waiting','done')),
  due_at TEXT NOT NULL, answered_at TEXT, decision TEXT NOT NULL DEFAULT '', decision_code TEXT NOT NULL DEFAULT '',
  executed INTEGER NOT NULL DEFAULT 0, executed_at TEXT, admin_note TEXT NOT NULL DEFAULT '',
  requester_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_legal_status ON legal_requests(status, due_at);
"""

# колонки, которые появились позже: (таблица, колонка, описание)
COLUMNS = (
    ("complaints", "reporter_email", "TEXT"),
    ("complaints", "reporter_key", "TEXT"),
    ("complaints", "reason_code", "TEXT"),
    ("complaints", "resolution", "TEXT"),
    ("complaints", "resolved_at", "TEXT"),
    ("complaints", "resolved_by", "INTEGER"),
    ("complaints", "admin_note", "TEXT"),
    ("complaints", "reply_sent_at", "TEXT"),
    ("reports", "resolution", "TEXT"),
    ("reports", "resolved_at", "TEXT"),
    ("reports", "resolved_by", "INTEGER"),
    ("reports", "admin_note", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing: dict[str, set[str]] = {}
    for table, column, ddl in COLUMNS:
        if table not in existing:
            existing[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing[table] and column not in existing[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_complaints_vac ON complaints(vacancy_id, status)")
    conn.commit()
