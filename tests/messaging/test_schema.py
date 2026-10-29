"""Схема откликов и переписки: новые колонки и индексы, миграция старой базы."""
import sqlite3

from rabota.db import connect
from rabota.messaging import schema


def cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def indexes(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_new_database_has_messaging_columns_and_indexes():
    c = connect(":memory:")
    assert {"contact_name", "contact_phone", "withdrawn_at", "status_changed_at"} <= cols(c, "applications")
    assert "title" in cols(c, "conversations")
    assert "snapshot" in cols(c, "reports")
    assert {"notify_responses", "notify_messages"} <= cols(c, "users")
    assert {"idx_app_user", "idx_conv_employer", "idx_conv_seeker", "idx_msg_unread", "idx_reports_target"} <= indexes(c)


def test_notification_flags_default_to_on():
    c = connect(":memory:")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('a@x.ru','h','employer')")
    row = c.execute("SELECT notify_responses, notify_messages FROM users").fetchone()
    assert (row[0], row[1]) == (1, 1)


def test_migration_upgrades_a_database_made_before_this_feature(tmp_path):
    """Старая база: таблицы есть, новых колонок нет. Данные остаются, колонки появляются."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, role TEXT, name TEXT DEFAULT '');
        CREATE TABLE applications (id INTEGER PRIMARY KEY, vacancy_id INTEGER, user_id INTEGER,
          cover_letter TEXT DEFAULT '', status TEXT DEFAULT 'new', created_at TEXT);
        CREATE TABLE conversations (id INTEGER PRIMARY KEY, employer_user_id INTEGER, seeker_user_id INTEGER,
          vacancy_id INTEGER, application_id INTEGER, started_by INTEGER, created_at TEXT, last_message_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, sender_user_id INTEGER,
          body TEXT, created_at TEXT, read_at TEXT);
        CREATE TABLE reports (id INTEGER PRIMARY KEY, reporter_user_id INTEGER, target_type TEXT, target_id INTEGER,
          reason TEXT, status TEXT DEFAULT 'new', created_at TEXT);
        INSERT INTO users(id, email, password_hash, role) VALUES (1, 'a@x.ru', 'h', 'employer');
        INSERT INTO applications(id, vacancy_id, user_id, cover_letter) VALUES (1, 5, 1, 'привет');
        """
    )
    old.row_factory = sqlite3.Row
    schema.migrate(old)
    assert {"contact_phone", "withdrawn_at"} <= cols(old, "applications")
    assert old.execute("SELECT cover_letter, contact_phone FROM applications").fetchone()[0] == "привет"
    assert old.execute("SELECT notify_messages FROM users").fetchone()[0] == 1
    schema.migrate(old)  # второй запуск ничего не ломает
    assert "idx_msg_unread" in indexes(old)


def test_connect_twice_is_safe(tmp_path):
    path = tmp_path / "x.db"
    connect(path).close()
    c = connect(path)
    assert "snapshot" in cols(c, "reports")
