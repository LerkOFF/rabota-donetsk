"""Фикстуры кабинета работодателя: база с аккаунтами и вакансиями владельца, время задаётся явно."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rabota.accounts import service
from tests.messaging.conftest import add_user

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "cabinet.db"
    service.ensure_schema(path)
    c = service.open_db(path)
    yield c
    c.close()


@pytest.fixture
def boss(conn):
    return add_user(conn, "boss@example.test", "employer", "Олег Босс")


@pytest.fixture
def other(conn):
    return add_user(conn, "other@example.test", "employer", "Игорь Чужой")


@pytest.fixture
def seeker(conn):
    return add_user(conn, "seeker@example.test", "job_seeker", "Мария Петрова")


def make_row(conn, owner_id: int | None, *, profession: str = "бариста", status: str = "active", moderation: str = "approved",
             seen: datetime | None = None, note: str | None = None, **extra) -> int:
    """Вакансия работодателя напрямую в SQL. seen: время последней публикации (по умолчанию час назад от NOW)."""
    seen = seen or NOW - timedelta(hours=1)
    stamp = seen.replace(microsecond=0).isoformat()
    cols = {
        "profession": profession, "title_raw": profession.capitalize(), "category": "общепит", "city": "Донецк",
        "source_type": "site", "source": "razmestit", "origin": "direct", "owner_user_id": owner_id,
        "moderation_status": moderation, "moderation_note": note, "status": status, "employer": "Кофейня Зёрна",
        "published_at": stamp, "last_seen_at": stamp, "summary": "Готовить кофе и общаться с гостями.",
        "contacts": '{"phones": ["+79490000000"]}', "pay_min": 30000, "pay_max": 40000, "pay_period": "месяц",
        "created_at": stamp,
    }
    cols.update(extra)
    names = ",".join(cols)
    cur = conn.execute(f"INSERT INTO vacancies({names}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
    conn.commit()
    return int(cur.lastrowid)
