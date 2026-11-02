"""Привязка Telegram к аккаунту: колонки у пользователя, коды привязки и запросы на вход.

Вызывается из rabota.db.connect, повтор безопасен: колонки добавляются только если их ещё нет, остальное IF NOT EXISTS.
Аккаунт создаётся только через почту, Telegram лишь привязывается к уже существующему аккаунту в профиле.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- Одноразовый код привязки из профиля: в базе только хеш, живёт недолго
CREATE TABLE IF NOT EXISTS tg_link_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_link_codes_user ON tg_link_codes(user_id);
-- Запрос на вход через Telegram: хеш кода из ссылки t.me/<бот>?start=login_<код>, статус и кто подтвердил.
-- status: pending (ждёт нажатия Start), confirmed, unlinked (Telegram не привязан), blocked, used (сессия выдана)
CREATE TABLE IF NOT EXISTS tg_login_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','unlinked','blocked','used')),
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, tg_user_id INTEGER,
  ip TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_at TEXT, used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tg_login_user ON tg_login_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_tg_login_expires ON tg_login_requests(expires_at);
"""

USER_COLUMNS = (
    ("tg_user_id", "INTEGER"),
    ("tg_username", "TEXT"),
    ("tg_linked_at", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for name, ddl in USER_COLUMNS:
        if name in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:  # соседний процесс успел добавить колонку первым
            if "duplicate column" not in str(exc).lower():
                raise
    # один Telegram - один аккаунт
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tg_user ON users(tg_user_id) WHERE tg_user_id IS NOT NULL"
    )
    conn.executescript(SCHEMA)
    conn.commit()
