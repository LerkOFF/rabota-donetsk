"""Фикстуры тестов откликов и переписки: чистая база, пользователи и вакансии напрямую в SQL."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rabota.accounts import passwords, service
from rabota.accounts.tokens import iso

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "msg.db"
    service.ensure_schema(path)
    c = service.open_db(path)
    yield c
    c.close()


def add_user(conn, email: str, role: str = "job_seeker", name: str = "Иван", phone: str | None = None,
             status: str = "active", verified: bool = True) -> dict:
    cur = conn.execute(
        "INSERT INTO users(email, password_hash, role, name, phone, status, email_verified_at, consent_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (email, passwords.hash_password("tulip-garden-77"), role, name, phone, status,
         iso(NOW) if verified else None, iso(NOW), iso(NOW)),
    )
    conn.commit()
    user = service.get_user(conn, int(cur.lastrowid))
    assert user is not None
    return user


def add_vacancy(conn, owner_id: int | None, *, origin: str = "direct", profession: str = "бариста",
                status: str = "active", moderation: str = "approved", employer: str = "Кофейня Зёрна") -> int:
    seen = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    cur = conn.execute(
        "INSERT INTO vacancies(profession, city, source_type, source, origin, owner_user_id, moderation_status,"
        " status, employer, published_at, last_seen_at, summary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (profession, "Донецк", "site", "razmestit", origin, owner_id, moderation, status, employer,
         seen.isoformat(), seen.isoformat(), "Готовить кофе."),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def seeker(conn):
    return add_user(conn, "seeker@example.test", "job_seeker", "Мария Петрова", "+79491112233")


@pytest.fixture
def employer(conn):
    return add_user(conn, "boss@example.test", "employer", "Олег Босс")


@pytest.fixture
def vacancy(conn, employer):
    return add_vacancy(conn, employer["id"])
