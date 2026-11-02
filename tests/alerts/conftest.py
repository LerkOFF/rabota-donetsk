from __future__ import annotations

import pytest

from rabota.web.queries import open_conn


@pytest.fixture
def conn(tmp_path):
    c = open_conn(tmp_path / "alerts.db")
    yield c
    c.close()
