"""Привязка Telegram и вход через бота: логика без веба (коды, сроки, одноразовость, привязка к браузеру)."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from rabota.accounts import service
from rabota.accounts import telegram as tg
from rabota.accounts.tokens import iso
from rabota.db import connect
from tests.alerts.helpers import NOW, user

SECRET = b"tg-test-secret-" + b"x" * 16


@pytest.fixture
def anna(conn):
    return user(conn, "anna@example.test")


def link(conn, u, tg_id=1001, name="anna_tg", now=NOW):
    code = tg.issue_link_code(conn, u["id"], now)
    return tg.bind_by_code(conn, code, tg_id, name, now)


# ------------------------------------------------------------------ схема

def test_migration_is_repeatable_and_adds_columns(tmp_path):
    path = tmp_path / "m.db"
    for _ in range(3):
        connect(path).close()
    c = connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    assert {"tg_user_id", "tg_username", "tg_linked_at"} <= cols
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"tg_link_codes", "tg_login_requests", "idx_users_tg_user"} <= names


def test_migration_adds_columns_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,"
        " role TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', phone TEXT, email_verified_at TEXT, status TEXT NOT NULL DEFAULT 'active',"
        " consent_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_login_at TEXT)"
    )
    raw.execute("INSERT INTO users(email, password_hash, role) VALUES ('a@x.ru', 'h', 'employer')")
    raw.commit()
    raw.close()
    c = connect(path)
    row = c.execute("SELECT email, tg_user_id FROM users").fetchone()
    assert row["email"] == "a@x.ru" and row["tg_user_id"] is None


def test_telegram_id_is_unique_per_account(conn):
    a, b = user(conn, "a@example.test"), user(conn, "b@example.test")
    conn.execute("UPDATE users SET tg_user_id=5 WHERE id=?", (a["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE users SET tg_user_id=5 WHERE id=?", (b["id"],))
    conn.execute("UPDATE users SET tg_user_id=NULL WHERE id=?", (b["id"],))  # пустых значений может быть сколько угодно
    conn.execute("UPDATE users SET tg_user_id=NULL WHERE id=?", (a["id"],))


# ------------------------------------------------------------------ коды привязки

def test_split_start_parameter():
    assert tg.split_start("link_abc") == ("link", "abc")
    assert tg.split_start("login_abc") == ("login", "abc")
    assert tg.split_start("Xy12Ab") == ("", "Xy12Ab")


def test_link_code_is_stored_only_as_hash(conn, anna):
    code = tg.issue_link_code(conn, anna["id"], NOW)
    rows = conn.execute("SELECT * FROM tg_link_codes").fetchall()
    assert len(rows) == 1 and code not in rows[0]["code_hash"] and rows[0]["code_hash"] == tg.hash_code(code)
    assert rows[0]["expires_at"] == iso(NOW + tg.LINK_TTL) and tg.LINK_TTL == timedelta(minutes=15)


def test_bind_links_account_and_cleans_username(conn, anna):
    status, found = link(conn, anna, 4242, "@Anna_TG")
    assert status == "ok" and found["id"] == anna["id"]
    row = conn.execute("SELECT * FROM users WHERE id=?", (anna["id"],)).fetchone()
    assert row["tg_user_id"] == 4242 and row["tg_username"] == "Anna_TG" and row["tg_linked_at"] == iso(NOW)
    assert tg.link_info(conn, anna["id"]) == {"linked": True, "username": "Anna_TG", "linked_at": iso(NOW), "can_unlink": True}


def test_bind_with_odd_username_stores_nothing_odd(conn, anna):
    link(conn, anna, 1, "<b>x</b>")
    assert conn.execute("SELECT tg_username FROM users").fetchone()[0] is None


def test_link_code_is_single_use(conn, anna):
    code = tg.issue_link_code(conn, anna["id"], NOW)
    assert tg.bind_by_code(conn, code, 7, "", NOW)[0] == "ok"
    assert tg.bind_by_code(conn, code, 8, "", NOW)[0] == "bad"
    assert conn.execute("SELECT tg_user_id FROM users").fetchone()[0] == 7


def test_link_code_expires_after_fifteen_minutes(conn, anna):
    code = tg.issue_link_code(conn, anna["id"], NOW)
    assert tg.bind_by_code(conn, code, 7, "", NOW + timedelta(minutes=15))[0] == "bad"
    code = tg.issue_link_code(conn, anna["id"], NOW)
    assert tg.bind_by_code(conn, code, 7, "", NOW + timedelta(minutes=14, seconds=59))[0] == "ok"


def test_new_code_cancels_the_previous_one(conn, anna):
    old = tg.issue_link_code(conn, anna["id"], NOW)
    new = tg.issue_link_code(conn, anna["id"], NOW)
    assert tg.bind_by_code(conn, old, 7, "", NOW)[0] == "bad"
    assert tg.bind_by_code(conn, new, 7, "", NOW)[0] == "ok"


@pytest.mark.parametrize("code", ["", "short", "../../etc/passwd", "a" * 80, "x y" * 8, "\x00" * 20])
def test_garbage_link_codes_are_rejected(conn, anna, code):
    assert tg.bind_by_code(conn, code, 7, "", NOW) == ("bad", None)


def test_telegram_of_another_account_is_refused(conn):
    a, b = user(conn, "a@example.test"), user(conn, "b@example.test")
    assert link(conn, a, 55)[0] == "ok"
    status, _ = link(conn, b, 55)
    assert status == "taken"
    assert conn.execute("SELECT tg_user_id FROM users WHERE id=?", (b["id"],)).fetchone()[0] is None


def test_account_with_other_telegram_is_refused_and_same_one_is_idempotent(conn, anna):
    assert link(conn, anna, 55)[0] == "ok"
    assert link(conn, anna, 66)[0] == "other"
    assert link(conn, anna, 55)[0] == "ok"
    assert conn.execute("SELECT tg_user_id FROM users").fetchone()[0] == 55


def test_blocked_account_cannot_link(conn, anna):
    code = tg.issue_link_code(conn, anna["id"], NOW)
    service.set_blocked(conn, anna["id"], True)
    assert tg.bind_by_code(conn, code, 7, "", NOW)[0] == "blocked"
    assert conn.execute("SELECT tg_user_id FROM users").fetchone()[0] is None


# ------------------------------------------------------------------ отвязка

def test_unlink_clears_the_link(conn, anna):
    link(conn, anna, 55)
    assert tg.unlink(conn, anna["id"], NOW) == "ok"
    row = conn.execute("SELECT * FROM users").fetchone()
    assert row["tg_user_id"] is None and row["tg_username"] is None and row["tg_linked_at"] is None
    assert tg.unlink(conn, anna["id"], NOW) == "not_linked"
    assert link(conn, user(conn, "b@example.test"), 55)[0] == "ok"  # освободившийся Telegram можно привязать к другому


def test_unlink_needs_a_way_to_sign_in(conn, anna):
    link(conn, anna, 55)
    conn.execute("UPDATE users SET password_hash='', email_verified_at=NULL WHERE id=?", (anna["id"],))
    assert tg.link_info(conn, anna["id"])["can_unlink"] is False
    assert tg.unlink(conn, anna["id"], NOW) == "no_login"
    assert conn.execute("SELECT tg_user_id FROM users").fetchone()[0] == 55
    conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (iso(NOW), anna["id"]))  # хватит и подтверждённой почты
    assert tg.unlink(conn, anna["id"], NOW) == "ok"


def test_unlink_cancels_a_confirmed_login_in_flight(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    assert tg.confirm_login(conn, code, 55, NOW)[0] == "ok"
    tg.unlink(conn, anna["id"], NOW)
    assert tg.complete_login(conn, SECRET, nonce, NOW)[0] == "expired"


# ------------------------------------------------------------------ вход

def test_login_code_is_derived_from_nonce_and_stored_as_hash(conn):
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    assert code == tg.code_for_nonce(SECRET, nonce) and len(code) == 32
    assert tg.code_for_nonce(b"other-secret" * 3, nonce) != code
    row = conn.execute("SELECT * FROM tg_login_requests").fetchone()
    assert row["code_hash"] == tg.hash_code(code) and code not in row["code_hash"] and nonce not in row["code_hash"]
    assert row["status"] == "pending" and row["expires_at"] == iso(NOW + timedelta(minutes=5))


def test_confirm_then_complete_gives_the_user_once(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    assert tg.login_state(conn, SECRET, nonce, NOW) == "waiting"
    assert tg.complete_login(conn, SECRET, nonce, NOW) == ("waiting", None)
    status, who = tg.confirm_login(conn, code, 55, NOW)
    assert status == "ok" and who["id"] == anna["id"] and "password_hash" not in who
    assert tg.login_state(conn, SECRET, nonce, NOW) == "confirmed"
    status, who = tg.complete_login(conn, SECRET, nonce, NOW)
    assert status == "ok" and who["id"] == anna["id"]
    assert tg.complete_login(conn, SECRET, nonce, NOW)[0] == "expired"  # второй раз забрать нельзя
    assert tg.confirm_login(conn, code, 55, NOW)[0] == "done"


def test_other_browser_cannot_take_the_login(conn, anna):
    """Код из ссылки известен всем, кто видел ссылку, а значение из cookie только тому браузеру, что начал вход."""
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 55, NOW)
    assert tg.complete_login(conn, SECRET, code, NOW) == ("none", None)  # код вместо значения из cookie
    assert tg.complete_login(conn, SECRET, "z" * 43, NOW) == ("none", None)
    assert tg.complete_login(conn, SECRET, "", NOW) == ("none", None)
    assert tg.complete_login(conn, SECRET, nonce, NOW)[0] == "ok"


def test_login_expires_after_five_minutes(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    late = NOW + timedelta(minutes=5)
    assert tg.confirm_login(conn, code, 55, late) == ("bad", None)
    assert tg.login_state(conn, SECRET, nonce, late) == "expired"
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 55, NOW + timedelta(minutes=4))
    assert tg.complete_login(conn, SECRET, nonce, late) == ("expired", None)  # подтверждённый, но не забранный вход тоже истекает


def test_unlinked_telegram_gets_no_session(conn, anna):
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    assert tg.confirm_login(conn, code, 999, NOW) == ("unlinked", None)
    assert tg.login_state(conn, SECRET, nonce, NOW) == "unlinked"
    assert tg.complete_login(conn, SECRET, nonce, NOW) == ("unlinked", None)
    assert conn.execute("SELECT COUNT(*) FROM users WHERE tg_user_id=999").fetchone()[0] == 0  # аккаунт не создаётся
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    link(conn, anna, 999)  # привязали в профиле и нажали Start ещё раз
    assert tg.confirm_login(conn, code, 999, NOW)[0] == "ok"


def test_blocked_account_cannot_confirm_or_complete(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    service.set_blocked(conn, anna["id"], True)
    assert tg.confirm_login(conn, code, 55, NOW)[0] == "blocked"
    assert tg.login_state(conn, SECRET, nonce, NOW) == "blocked"
    assert tg.complete_login(conn, SECRET, nonce, NOW) == ("blocked", None)


def test_account_blocked_between_confirm_and_complete(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 55, NOW)
    service.set_blocked(conn, anna["id"], True)
    assert tg.complete_login(conn, SECRET, nonce, NOW) == ("blocked", None)


def test_unverified_email_cannot_sign_in_through_telegram(conn, anna):
    link(conn, anna, 55)
    conn.execute("UPDATE users SET email_verified_at=NULL")
    _nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    assert tg.confirm_login(conn, code, 55, NOW)[0] == "blocked"


def test_relinked_telegram_invalidates_a_confirmed_login(conn, anna):
    link(conn, anna, 55)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 55, NOW)
    conn.execute("UPDATE users SET tg_user_id=77")
    assert tg.complete_login(conn, SECRET, nonce, NOW) == ("blocked", None)


def test_admin_can_sign_in_like_with_password(conn):
    admin = user(conn, "root@example.test", role="admin")
    link(conn, admin, 9)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 9, NOW)
    status, who = tg.complete_login(conn, SECRET, nonce, NOW)
    assert status == "ok" and who["is_admin"]


@pytest.mark.parametrize("code", ["", "short", "a" * 99, "../../x", "x" * 31 + "!"])
def test_garbage_login_codes_are_bad(conn, code):
    assert tg.confirm_login(conn, code, 1, NOW) == ("bad", None)


def test_unknown_code_is_bad(conn):
    assert tg.confirm_login(conn, "A" * 32, 1, NOW) == ("bad", None)


def test_purge_removes_old_records(conn, anna):
    tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.issue_link_code(conn, anna["id"], NOW)
    tg.purge(conn, NOW + timedelta(days=2))
    assert conn.execute("SELECT COUNT(*) FROM tg_login_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tg_link_codes").fetchone()[0] == 0


def test_deleting_account_removes_codes_and_requests(conn, anna):
    link(conn, anna, 55)
    tg.issue_link_code(conn, anna["id"], NOW)
    nonce, code = tg.start_login(conn, SECRET, "1.2.3.4", NOW)
    tg.confirm_login(conn, code, 55, NOW)
    service.delete_account(conn, anna["id"])
    assert conn.execute("SELECT COUNT(*) FROM tg_link_codes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tg_login_requests").fetchone()[0] == 0
    assert tg.complete_login(conn, SECRET, nonce, NOW)[0] in ("none", "expired")


def test_available_needs_token_and_enabled_setting(conn, monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    assert tg.available(conn) is False
    monkeypatch.setenv("TG_BOT_TOKEN", "1:t")
    monkeypatch.setenv("TG_BOT_USERNAME", "my_bot")
    assert tg.available(conn) is True
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('tg_bot_enabled', '0', 'x')")
    conn.commit()
    assert tg.available(conn) is False
