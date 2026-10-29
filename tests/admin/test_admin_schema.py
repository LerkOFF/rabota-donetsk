"""Схема служебной части: таблицы и колонки появляются при connect и не ломаются при повторном запуске."""
from rabota.db import connect


def test_tables_created(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"audit_log", "stoplist", "legal_requests", "complaints", "reports"} <= names


def test_complaints_get_admin_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(complaints)")}
    assert {"reporter_email", "reporter_key", "reason_code", "resolution", "resolved_at", "admin_note",
            "reply_sent_at"} <= cols


def test_reports_get_admin_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
    assert {"resolution", "resolved_at", "admin_note"} <= cols


def test_migrate_is_idempotent(db_path):
    connect(db_path).close()
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_old_complaint_rows_survive(db_path):
    import sqlite3

    raw = sqlite3.connect(db_path)
    raw.executescript(
        "CREATE TABLE complaints (id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id INTEGER NOT NULL, "
        "reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
        "INSERT INTO complaints(vacancy_id, reason) VALUES (7, 'Другое: тест');"
    )
    raw.commit()
    raw.close()
    c = connect(db_path)
    r = c.execute("SELECT reason, reporter_email, reason_code FROM complaints").fetchone()
    assert r["reason"] == "Другое: тест" and r["reporter_email"] is None
