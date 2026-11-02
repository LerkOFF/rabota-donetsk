"""Миграция автокарточек. Вызывается из rabota.db.connect, повторный запуск безопасен.

- companies: владелец может быть пустым (у автокарточки его нет), новые колонки и уникальные индексы;
- vacancies: company_state (результат привязки), archived_at (когда ушла в архив), owner_edited_at (правка владельца);
- company_claims: заявки «Это моя компания» (история действий идёт в общий журнал audit_log).
"""
from __future__ import annotations

import re
import sqlite3

COMPANY_COLUMNS = (
    ("origin", "TEXT NOT NULL DEFAULT 'registered'"),
    ("norm_name", "TEXT"),
    ("legal_form", "TEXT"),
    ("source_kind", "TEXT"),
    ("source_ref", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_vacancy_at", "TEXT"),
    ("claimed_at", "TEXT"),
    ("merged_into", "INTEGER"),
)

VACANCY_COLUMNS = (
    ("company_state", "TEXT"),
    ("archived_at", "TEXT"),
    ("owner_edited_at", "TEXT"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','review','confirmed','rejected')),
  method TEXT NOT NULL CHECK (method IN ('email','phone')),
  contact TEXT NOT NULL, email_domain TEXT NOT NULL DEFAULT '',
  domain_match INTEGER NOT NULL DEFAULT 0, phone_match INTEGER NOT NULL DEFAULT 0,
  code_hash TEXT, code_expires_at TEXT, code_attempts INTEGER NOT NULL DEFAULT 0,
  code_sent_at TEXT, code_sent_count INTEGER NOT NULL DEFAULT 0, code_ok_at TEXT,
  dispute INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
  docs_requested_at TEXT, docs_note TEXT, admin_note TEXT NOT NULL DEFAULT '',
  decided_by INTEGER, decided_at TEXT, ip TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_company ON company_claims(company_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_user ON company_claims(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_claims_status ON company_claims(status, created_at);
"""


def _owner_is_required(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA table_info(companies)"):
        if row[1] == "owner_user_id":
            return bool(row[3])
    return False


def _relax_owner(conn: sqlite3.Connection) -> None:
    """У автокарточки владельца нет: пересоздать таблицу companies с необязательным owner_user_id.

    Данные, колонки и индексы сохраняются; связи вакансий не трогаются (внешние ключи на время выключены)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='companies'").fetchone()
    if row is None or not row[0]:
        return
    ddl = re.sub(r"owner_user_id\s+INTEGER\s+NOT\s+NULL", "owner_user_id INTEGER", row[0], count=1, flags=re.IGNORECASE)
    if ddl == row[0]:
        return
    ddl = re.sub(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?\"?companies\"?", "CREATE TABLE companies__new", ddl, count=1, flags=re.IGNORECASE)
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='companies' AND sql IS NOT NULL")]
    columns = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(companies)"))
    conn.commit()
    fk_before = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS companies__new")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO companies__new({columns}) SELECT {columns} FROM companies")
        conn.execute("DROP TABLE companies")
        conn.execute("ALTER TABLE companies__new RENAME TO companies")
        for sql in indexes:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if fk_before else 'OFF'}")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if cols:
        if _owner_is_required(conn):
            _relax_owner(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        for name, ddl in COMPANY_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {name} {ddl}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_auto_norm ON companies(norm_name, COALESCE(city, '')) "
            "WHERE origin = 'auto' AND norm_name IS NOT NULL AND merged_into IS NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_auto_ref ON companies(source_kind, source_ref) "
            "WHERE origin = 'auto' AND source_ref IS NOT NULL AND merged_into IS NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_origin ON companies(origin, owner_user_id)")
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(vacancies)")}
    if vcols:
        for name, ddl in VACANCY_COLUMNS:
            if name not in vcols:
                conn.execute(f"ALTER TABLE vacancies ADD COLUMN {name} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vac_company ON vacancies(company_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vac_status_seen ON vacancies(status, last_seen_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vac_company_pending ON vacancies(id) "
            "WHERE company_id IS NULL AND company_state IS NULL AND employer IS NOT NULL"
        )
    conn.executescript(SCHEMA)
    conn.commit()
