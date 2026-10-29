import sqlite3

import pytest

from rabota.db import connect


def test_account_tables_exist():
    c = connect(":memory:")
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "email_tokens", "sessions", "companies", "resumes", "applications", "favorites",
              "conversations", "messages", "user_blocks", "reports"):
        assert t in names


def test_vacancy_owner_columns_and_defaults():
    c = connect(":memory:")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(vacancies)")}
    assert {"owner_user_id", "company_id", "origin", "moderation_status", "moderation_note"} <= cols
    c.execute("INSERT INTO vacancies(profession) VALUES ('бариста')")
    row = c.execute("SELECT origin, moderation_status FROM vacancies").fetchone()
    assert (row["origin"], row["moderation_status"]) == ("parsed", "approved")


def test_user_email_unique_case_insensitive_and_role_check():
    c = connect(":memory:")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('A@x.ru', 'h', 'employer')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO users(email, password_hash, role) VALUES ('a@X.ru', 'h', 'employer')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO users(email, password_hash, role) VALUES ('b@x.ru', 'h', 'hacker')")


def test_pending_vacancy_is_not_visible():
    from rabota.web import queries

    c = connect(":memory:")
    c.execute("INSERT INTO vacancies(profession, kind, status, moderation_status) VALUES ('а','vacancy','active','pending')")
    c.execute("INSERT INTO vacancies(profession, kind, status) VALUES ('б','vacancy','active')")
    names = [r[0] for r in c.execute(f"SELECT profession FROM vacancies WHERE {queries.VISIBLE_SQL}")]
    assert names == ["б"]
