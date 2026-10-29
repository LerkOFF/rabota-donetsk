"""Фикстуры тестов картинок."""
from __future__ import annotations

import pytest
from vision_helpers import FIXTURES


@pytest.fixture()
def poster_bytes() -> bytes:
    return (FIXTURES / "poster_moloko.jpg").read_bytes()


@pytest.fixture()
def wall_bytes() -> bytes:
    return (FIXTURES / "ad_wall_finishing.jpg").read_bytes()
