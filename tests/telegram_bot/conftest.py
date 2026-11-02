from __future__ import annotations

import pytest

from rabota.web.queries import open_conn


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "bot.db"


@pytest.fixture
def conn(db_path):
    c = open_conn(db_path)
    yield c
    c.close()
