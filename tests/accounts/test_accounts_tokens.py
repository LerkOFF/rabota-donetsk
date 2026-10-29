import sqlite3
from datetime import timedelta

import pytest

from rabota.accounts import tokens
from tests.accounts.harness import START


@pytest.fixture
def uid(conn):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('t@example.test', 'h', 'job_seeker')")
    conn.commit()
    return conn.execute("SELECT id FROM users").fetchone()["id"]


def test_token_is_random_and_urlsafe():
    a, b = tokens.new_token(), tokens.new_token()
    assert a != b and len(a) >= 40
    assert all(c.isalnum() or c in "-_" for c in a)


def test_only_sha256_hash_is_stored(conn, uid):
    raw = tokens.create_email_token(conn, uid, "verify", START)
    row = conn.execute("SELECT * FROM email_tokens").fetchone()
    assert row["token_hash"] == tokens.hash_token(raw)
    assert len(row["token_hash"]) == 64 and raw not in row["token_hash"]
    assert raw not in str(tuple(row))


@pytest.mark.parametrize("purpose,ttl", [("verify", timedelta(hours=24)), ("reset", timedelta(hours=1)), ("change", timedelta(hours=24))])
def test_ttl_by_purpose(conn, uid, purpose, ttl):
    raw = tokens.create_email_token(conn, uid, purpose, START, new_email="n@example.test" if purpose == "change" else None)
    assert tokens.peek_email_token(conn, raw, purpose, START + ttl - timedelta(seconds=1))
    assert tokens.peek_email_token(conn, raw, purpose, START + ttl) is None


def test_consume_is_single_use(conn, uid):
    raw = tokens.create_email_token(conn, uid, "verify", START)
    first = tokens.consume_email_token(conn, raw, "verify", START)
    assert first and first["user_id"] == uid
    assert tokens.consume_email_token(conn, raw, "verify", START) is None
    assert tokens.peek_email_token(conn, raw, "verify", START) is None


def test_expired_token_cannot_be_consumed(conn, uid):
    raw = tokens.create_email_token(conn, uid, "reset", START)
    assert tokens.consume_email_token(conn, raw, "reset", START + timedelta(hours=2)) is None


def test_purpose_must_match(conn, uid):
    raw = tokens.create_email_token(conn, uid, "verify", START)
    assert tokens.consume_email_token(conn, raw, "reset", START) is None
    assert tokens.consume_email_token(conn, raw, "verify", START) is not None


def test_peek_does_not_consume(conn, uid):
    raw = tokens.create_email_token(conn, uid, "reset", START)
    assert tokens.peek_email_token(conn, raw, "reset", START)
    assert tokens.peek_email_token(conn, raw, "reset", START)
    assert tokens.consume_email_token(conn, raw, "reset", START)


def test_new_token_cancels_previous_of_same_purpose(conn, uid):
    old = tokens.create_email_token(conn, uid, "verify", START)
    reset = tokens.create_email_token(conn, uid, "reset", START)
    new = tokens.create_email_token(conn, uid, "verify", START)
    assert tokens.consume_email_token(conn, old, "verify", START) is None
    assert tokens.consume_email_token(conn, new, "verify", START)
    assert tokens.consume_email_token(conn, reset, "reset", START)  # другое назначение не тронуто


@pytest.mark.parametrize("junk", ["", "x", "a" * 500, "' OR 1=1 --"])
def test_unknown_tokens_rejected(conn, uid, junk):
    assert tokens.peek_email_token(conn, junk, "verify", START) is None
    assert tokens.consume_email_token(conn, junk, "verify", START) is None


def test_unknown_purpose_raises(conn, uid):
    with pytest.raises(ValueError):
        tokens.create_email_token(conn, uid, "nope", START)


def test_change_token_keeps_new_email(conn, uid):
    raw = tokens.create_email_token(conn, uid, "change", START, new_email="n@example.test")
    assert tokens.consume_email_token(conn, raw, "change", START)["new_email"] == "n@example.test"


def test_purge_removes_expired_and_old_used(conn, uid):
    keep = tokens.create_email_token(conn, uid, "verify", START)
    tokens.create_email_token(conn, uid, "reset", START)  # просрочится через час
    later = START + timedelta(hours=3)
    removed = tokens.purge_expired_tokens(conn, later)
    assert removed == 1
    assert tokens.peek_email_token(conn, keep, "verify", later)


def test_token_hash_is_unique_in_db(conn, uid):
    conn.execute("INSERT INTO email_tokens(user_id, token_hash, purpose, expires_at) VALUES (?, 'h', 'verify', 'x')", (uid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO email_tokens(user_id, token_hash, purpose, expires_at) VALUES (?, 'h', 'verify', 'x')", (uid,))
