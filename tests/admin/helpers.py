"""Помощники тестов админки."""
from __future__ import annotations

from datetime import UTC, datetime

from rabota.models import Vacancy

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
ADMIN = {"id": 1, "email": "admin@example.test", "role": "admin"}


def make_vac(**kw) -> Vacancy:
    kw.setdefault("profession", "бариста")
    kw.setdefault("city", "Донецк")
    kw.setdefault("category", "общепит")
    kw.setdefault("summary", "Готовить кофе и общаться с гостями.")
    kw.setdefault("contacts", {"phones": ["+79490000000"]})
    kw.setdefault("source_type", "telegram")
    kw.setdefault("source", "@demo")
    kw.setdefault("source_url", "https://t.me/demo/1")
    kw.setdefault("published_at", "2026-10-29T11:30:00+00:00")
    return Vacancy(**kw)


def row(conn, vid: int):
    return conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
