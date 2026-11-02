"""Таблицы резюме поверх базовой схемы. Вызывается из rabota.db.connect, повтор безопасен.

Старая таблица resumes была рассчитана на одно резюме на человека (UNIQUE по user_id). Если такая ещё есть,
она пересоздаётся без этого ограничения, строки переносятся. Остальное добавляется колонками.
"""
from __future__ import annotations

import sqlite3

RESUMES_DDL = """
CREATE TABLE resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '', profession TEXT, city TEXT, district TEXT,
  pay_min INTEGER, pay_period TEXT, work_formats TEXT NOT NULL DEFAULT '[]', about TEXT NOT NULL DEFAULT '',
  experience TEXT NOT NULL DEFAULT '[]', skills TEXT NOT NULL DEFAULT '[]',
  visibility TEXT NOT NULL DEFAULT 'hidden' CHECK (visibility IN ('hidden','public','open')),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# (таблица, колонка, описание): добавляются только если колонки ещё нет
COLUMNS = (
    ("resumes", "relocate", "INTEGER NOT NULL DEFAULT 0"),
    ("resumes", "remote", "INTEGER NOT NULL DEFAULT 0"),
    ("resumes", "employments", "TEXT NOT NULL DEFAULT '[]'"),
    ("resumes", "schedule", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "experience_years", "INTEGER"),
    ("resumes", "contact_phone", "TEXT"),
    ("resumes", "contact_telegram", "TEXT"),
    # согласие на передачу работодателям: когда дано, по какой редакции текста, когда отозвано (резюме скрыто)
    ("resumes", "consent_at", "TEXT"),
    ("resumes", "consent_version", "TEXT"),
    ("resumes", "consent_withdrawn_at", "TEXT"),
    # скрытие модератором: владелец вернуть резюме в поиск сам не может
    ("resumes", "admin_hidden", "INTEGER NOT NULL DEFAULT 0"),
    ("resumes", "admin_note", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "admin_hidden_at", "TEXT"),
    ("resumes", "created_at", "TEXT"),
    # нормализованные (нижний регистр, е вместо ё) район и навыки: поиск без учёта регистра по кириллице
    ("resumes", "district_key", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "skills_key", "TEXT NOT NULL DEFAULT ''"),
    # какое резюме приложено к отклику и когда работодатель его просмотрел
    ("applications", "resume_id", "INTEGER REFERENCES resumes(id) ON DELETE SET NULL"),
    ("applications", "viewed_at", "TEXT"),
)

VIEWS_DDL = """
CREATE TABLE IF NOT EXISTS resume_views (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  employer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('view','contact')),
  at TEXT NOT NULL
)
"""

INDEXES = (
    ("idx_resumes_user", "resumes", "user_id, updated_at"),
    ("idx_resumes_search", "resumes", "visibility, admin_hidden, updated_at"),
    ("idx_resume_views_resume", "resume_views", "resume_id, kind, employer_user_id"),
    ("idx_resume_views_employer", "resume_views", "employer_user_id, kind, at"),
    ("idx_app_resume", "applications", "resume_id"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_unique_user(conn: sqlite3.Connection) -> bool:
    for idx in conn.execute("PRAGMA index_list(resumes)").fetchall():
        if idx[2] and [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")] == ["user_id"]:
            return True
    return False


def _rebuild(conn: sqlite3.Connection) -> None:
    """Снять UNIQUE(user_id): пересоздать таблицу и перенести строки."""
    cols = [c for c in _columns(conn, "resumes")]
    conn.execute("ALTER TABLE resumes RENAME TO resumes_legacy")
    conn.execute(RESUMES_DDL)
    keep = ", ".join(c for c in cols if c in _columns(conn, "resumes"))
    conn.execute(f"INSERT INTO resumes({keep}) SELECT {keep} FROM resumes_legacy")
    conn.execute("DROP TABLE resumes_legacy")


def migrate(conn: sqlite3.Connection) -> None:
    if not _columns(conn, "resumes"):
        conn.execute(RESUMES_DDL)
    elif _has_unique_user(conn):
        _rebuild(conn)
    for table, column, ddl in COLUMNS:
        have = _columns(conn, table)
        if have and column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.execute(VIEWS_DDL)
    for name, table, columns in INDEXES:
        if _columns(conn, table):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    conn.commit()
