"""Общие фикстуры тестов админки: временная база и вакансии."""
from __future__ import annotations

from pathlib import Path

import pytest

from rabota.db import connect, save_vacancy
from tests.admin.helpers import make_vac


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "admin.db"


@pytest.fixture
def conn(db_path: Path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def add(conn):
    """add(profession=..., moderation_status=..., ...) -> id."""

    def _add(moderation_status: str | None = None, **kw) -> int:
        vid = save_vacancy(conn, make_vac(**kw))
        if moderation_status:
            conn.execute("UPDATE vacancies SET moderation_status=? WHERE id=?", (moderation_status, vid))
            conn.commit()
        return vid

    return _add
