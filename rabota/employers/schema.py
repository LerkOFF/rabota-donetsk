"""Таблицы и колонки раздела работодателей. Вызывается из rabota.db.connect, повторный запуск безопасен."""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- Черновик вакансии гостя: хранится, пока почта не подтверждена кодом. В ленту и модерацию не попадает.
CREATE TABLE IF NOT EXISTS post_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE, email TEXT NOT NULL COLLATE NOCASE, payload TEXT NOT NULL,
  code_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, sent_count INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL, last_sent_at TEXT NOT NULL,
  consent_at TEXT NOT NULL, ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_post_drafts_email ON post_drafts(email, created_at);
"""

# колонки, которые появились позже: (таблица, колонка, описание)
COLUMNS = (
    ("companies", "verified_at", "TEXT"),
    ("companies", "verified_by", "INTEGER"),
    ("companies", "verify_note", "TEXT"),
    ("companies", "blocked_at", "TEXT"),
    ("companies", "blocked_note", "TEXT"),
    ("vacancies", "automod_flags", "TEXT"),
    ("vacancies", "raised_at", "TEXT"),
    ("vacancies", "raise_count", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing: dict[str, set[str]] = {}
    for table, column, ddl in COLUMNS:
        if table not in existing:
            existing[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing[table] and column not in existing[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    if existing.get("companies"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_verified ON companies(verified_at)")
        # компании, отмеченные раньше признаком verified, получают дату проверки
        conn.execute("UPDATE companies SET verified_at = created_at WHERE verified = 1 AND verified_at IS NULL")
    conn.commit()
