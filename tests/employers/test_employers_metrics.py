"""Показатели работодателя: показываются, только если данных достаточно; миграция повторяема."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rabota.db import connect
from rabota.employers import metrics, schema

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def at(**kw) -> str:
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ чистые функции

@pytest.mark.parametrize("seconds,text", [
    (None, ""), (-5, ""), (0, "в течение часа"), (3599, "в течение часа"), (3 * 3600, "около 3 часов"),
    (2 * 3600, "около 2 часов"), (24 * 3600, "около 1 дня"), (3 * 86400, "около 3 дней"),
])
def test_duration_text(seconds, text):
    assert metrics.duration_text(seconds) == text


def test_average_is_median_and_skips_broken_pairs():
    pairs = [("2026-10-01 10:00:00", "2026-10-01 11:00:00"), ("2026-10-01 10:00:00", "2026-10-01 12:00:00"),
             ("2026-10-01 10:00:00", "2026-11-01 10:00:00"),  # один ответ через месяц не портит картину
             ("2026-10-02 10:00:00", "2026-10-01 10:00:00"),  # ответ раньше отклика
             ("мусор", "2026-10-01 10:00:00")]
    assert metrics.average_seconds(pairs) == 2 * 3600
    assert metrics.average_seconds([]) is None


def test_stats_visibility_rules():
    assert not metrics.Stats().visible
    assert metrics.Stats(responses=1).visible and metrics.Stats(responses=1).show_responses
    two = metrics.Stats(responses=5, answered=2, avg_seconds=3600)
    assert not two.show_reply_time and two.reply_text == ""
    three = metrics.Stats(responses=5, answered=3, avg_seconds=3 * 3600)
    assert three.show_reply_time and three.reply_text == "около 3 часов"


# ------------------------------------------------------------------ по базе

@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "m.db")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('o@example.test', '!', 'employer')")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('s@example.test', '!', 'job_seeker')")
    c.execute("INSERT INTO companies(owner_user_id, name) VALUES (1, 'Кофейня')")
    c.execute("INSERT INTO companies(owner_user_id, name) VALUES (1, 'Другая')")
    c.execute(
        "INSERT INTO vacancies(title_raw, profession, city, source_type, source, external_id, dedupe_key, company_id) "
        "VALUES ('x', 'бариста', 'Донецк', 'site', 'razmestit', 'e1', 'd1', 1)")
    c.commit()
    yield c
    c.close()


def respond(conn, user_n, created, status="new", changed=None, withdrawn=None):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES (?, '!', 'job_seeker')", (f"u{user_n}@example.test",))
    uid = conn.execute("SELECT MAX(id) FROM users").fetchone()[0]
    conn.execute(
        "INSERT INTO applications(vacancy_id, user_id, status, created_at, status_changed_at, withdrawn_at) VALUES (1,?,?,?,?,?)",
        (uid, status, created, changed, withdrawn))
    conn.commit()


def test_no_data_no_stats(conn):
    s = metrics.employer_stats(conn, 1, NOW)
    assert (s.responses, s.answered, s.avg_seconds) == (0, 0, None) and not s.visible
    assert not metrics.employer_stats(conn, 0, NOW).visible


def test_counts_responses_and_reply_time(conn):
    respond(conn, 1, at(days=3), "viewed", at(days=3) .replace("12:00", "13:00"))
    respond(conn, 2, at(days=2), "invited", at(days=2).replace("12:00", "15:00"))
    respond(conn, 3, at(days=2), "rejected", at(days=2).replace("12:00", "14:00"))
    respond(conn, 4, at(days=1))  # без ответа
    s = metrics.employer_stats(conn, 1, NOW)
    assert (s.responses, s.answered) == (4, 3)
    assert s.avg_seconds == 2 * 3600 and s.show_reply_time and s.reply_text == "около 2 часов"


def test_reply_time_hidden_until_three_answers(conn):
    respond(conn, 1, at(days=3), "viewed", at(days=3).replace("12:00", "13:00"))
    respond(conn, 2, at(days=1))
    s = metrics.employer_stats(conn, 1, NOW)
    assert s.show_responses and not s.show_reply_time


def test_withdrawn_old_and_foreign_responses_are_not_counted(conn):
    respond(conn, 1, at(days=2), "new", None, withdrawn=at(days=1))
    respond(conn, 2, at(days=200))  # старше окна
    assert not metrics.employer_stats(conn, 1, NOW).visible
    assert metrics.employer_stats(conn, 2, NOW).responses == 0  # у другой компании своих откликов нет


# ------------------------------------------------------------------ миграция

def test_migration_is_repeatable_and_adds_columns(conn):
    for _ in range(2):
        schema.migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    assert {"verified_at", "verified_by", "verify_note", "blocked_at", "blocked_note"} <= cols
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(vacancies)")}
    assert {"automod_flags", "raised_at", "raise_count"} <= vcols
    assert conn.execute("SELECT COUNT(*) FROM post_drafts").fetchone()[0] == 0


def test_legacy_verified_company_gets_verification_date(tmp_path):
    c = connect(tmp_path / "l.db")
    c.execute("INSERT INTO users(email, password_hash, role) VALUES ('o@example.test', '!', 'employer')")
    c.execute("INSERT INTO companies(owner_user_id, name, verified, created_at) VALUES (1, 'Старая', 1, '2026-01-02 10:00:00')")
    c.commit()
    schema.migrate(c)
    assert c.execute("SELECT verified_at FROM companies").fetchone()[0] == "2026-01-02 10:00:00"
    c.close()
