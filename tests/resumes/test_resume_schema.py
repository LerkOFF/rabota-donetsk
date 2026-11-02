"""Таблицы резюме: свежая база, старая база с UNIQUE по пользователю, повторный запуск."""
import sqlite3

from rabota.accounts import service as accounts
from rabota.db import connect
from rabota.resumes import schema


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_has_all_columns_and_tables(tmp_path):
    conn = connect(tmp_path / "a.db")
    assert {"relocate", "remote", "employments", "schedule", "experience_years", "contact_phone", "contact_telegram",
            "consent_at", "consent_version", "consent_withdrawn_at", "admin_hidden", "admin_note", "district_key",
            "skills_key"} <= cols(conn, "resumes")
    assert {"resume_id", "viewed_at"} <= cols(conn, "applications")
    assert cols(conn, "resume_views")


def test_several_resumes_per_user_are_allowed_by_schema(tmp_path):
    conn = connect(tmp_path / "a.db")
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('a@example.test', '!', 'job_seeker')")
    for _ in range(3):
        conn.execute("INSERT INTO resumes(user_id, title) VALUES (1, 'x')")
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 3


def test_migration_is_repeatable(tmp_path):
    conn = connect(tmp_path / "a.db")
    schema.migrate(conn)
    schema.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0


def test_legacy_table_with_unique_user_is_rebuilt_and_rows_kept(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', phone TEXT, email_verified_at TEXT,
          status TEXT NOT NULL DEFAULT 'active', consent_at TEXT, created_at TEXT, last_login_at TEXT);
        INSERT INTO users(email, password_hash, role) VALUES ('a@example.test', '!', 'job_seeker');
        CREATE TABLE resumes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL DEFAULT '', profession TEXT, city TEXT, district TEXT, pay_min INTEGER, pay_period TEXT,
          work_formats TEXT NOT NULL DEFAULT '[]', about TEXT NOT NULL DEFAULT '', experience TEXT NOT NULL DEFAULT '[]',
          skills TEXT NOT NULL DEFAULT '[]', visibility TEXT NOT NULL DEFAULT 'hidden'
            CHECK (visibility IN ('hidden','public','open')), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO resumes(user_id, title, profession, visibility) VALUES (1, 'Бариста', 'бариста', 'public');
        """
    )
    raw.commit()
    raw.close()
    conn = connect(path)
    row = conn.execute("SELECT title, visibility FROM resumes").fetchone()
    assert (row["title"], row["visibility"]) == ("Бариста", "public")
    conn.execute("INSERT INTO resumes(user_id, title) VALUES (1, 'Второе')")
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 2
    assert "resumes_legacy" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.commit()
    conn.close()
    accounts.ensure_schema(path)  # повторное открытие ничего не ломает
