"""Общие фикстуры тестов сайта: временная база, клиент, фабрика вакансий."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rabota.db import save_vacancy
from rabota.models import Vacancy
from rabota.web.app import create_app
from rabota.web.queries import open_conn

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: сквозные тесты в браузере (Playwright, Chromium)")


def make_vacancy(**kw) -> Vacancy:
    """Вакансия с разумными значениями по умолчанию, поля переопределяются."""
    minutes = kw.pop("ago", 30)
    kw.setdefault("profession", "бариста")
    kw.setdefault("city", "Донецк")
    kw.setdefault("category", "общепит")
    kw.setdefault("summary", "Готовить кофе и общаться с гостями.")
    kw.setdefault("contacts", {"phones": ["+79490000000"]})
    kw.setdefault("source_type", "telegram")
    kw.setdefault("source", "@demo")
    kw.setdefault("source_url", "https://t.me/demo/1")
    kw.setdefault("published_at", (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds"))
    return Vacancy(**kw)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path):
    c = open_conn(db_path)
    yield c
    c.close()


@pytest.fixture
def add(conn):
    """add(profession=..., ...) -> id вставленной вакансии."""

    def _add(**kw) -> int:
        return save_vacancy(conn, make_vacancy(**kw))

    return _add


@pytest.fixture
def app(db_path: Path):
    return create_app(db_path, site_url="https://example.test", now=lambda: NOW)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c
