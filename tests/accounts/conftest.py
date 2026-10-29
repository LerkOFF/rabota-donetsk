"""Фикстуры тестов аккаунтов: чистая база с готовой схемой."""
from __future__ import annotations

from pathlib import Path

import pytest

from rabota.accounts import service


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "svc.db"
    service.ensure_schema(path)
    c = service.open_db(path)
    yield c
    c.close()
