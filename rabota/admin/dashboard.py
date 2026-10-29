"""Сводка для главной страницы админки: счётчики, источники с проблемами, последние ошибки."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rabota.admin import audit, complaints, moderation, stoplist
from rabota.admin import requests as legal_requests
from rabota.admin.common import now_utc
from rabota.lifecycle import parse_iso
from rabota.web import queries


def default_health(conn: sqlite3.Connection) -> Any:
    from rabota.realtime.health import check_health

    return check_health(conn)


def source_problems(conn: sqlite3.Connection, health: Callable[[sqlite3.Connection], Any] | None, now: datetime) -> dict[str, Any]:
    """Источники, которые давно не отвечали, и состояние воркера. Сбой проверки не роняет страницу."""
    try:
        report = (health or default_health)(conn)
    except Exception as exc:  # noqa: BLE001 - админка должна открываться, даже если проверка не работает
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "worker": "unknown", "problems": [], "total": 0}
    problems = [
        {"key": s.key, "label": s.label, "status": s.status, "age": s.age_seconds, "error": s.last_error}
        for s in report.sources if s.problem
    ]
    return {"ok": report.ok, "error": "", "worker": report.worker_status, "worker_age": report.worker_age_seconds,
            "problems": problems, "total": len(report.sources)}


def recent_errors(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    """Последние ошибки приёма данных из состояния источников."""
    rows = conn.execute(
        "SELECT source, last_error, updated_at FROM ingest_state WHERE last_error IS NOT NULL AND last_error != '' "
        "ORDER BY updated_at DESC LIMIT ?", (limit,),
    ).fetchall()
    return [{"source": r["source"], "error": r["last_error"], "at": r["updated_at"]} for r in rows]


def build(conn: sqlite3.Connection, now: datetime | None = None, health: Callable[[sqlite3.Connection], Any] | None = None) -> dict[str, Any]:
    now = now or now_utc()
    mod = moderation.counts(conn)
    cmp_ = complaints.counts(conn)
    req = legal_requests.counts(conn, now)
    active = int(conn.execute(f"SELECT COUNT(*) FROM vacancies WHERE {queries.VISIBLE_SQL}").fetchone()[0])
    autohidden = int(conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE status='hidden' AND moderation_note LIKE 'Скрыто автоматически%'").fetchone()[0])
    sources = source_problems(conn, health, now)
    return {
        "active": active,
        "pending": mod["pending"],
        "complaints_open": cmp_["open"],
        "reports_open": cmp_["reports_open"],
        "autohidden": autohidden,
        "requests_open": req["open"],
        "requests_overdue": req["overdue"],
        "stoplist": stoplist.count(conn),
        "sources": sources,
        "source_problems": len(sources["problems"]) + (1 if sources["worker"] not in ("ok",) else 0),
        "errors": recent_errors(conn),
        "audit": audit.recent(conn, 6),
        "oldest_pending": _oldest(conn, "SELECT MIN(created_at) FROM vacancies WHERE moderation_status='pending'", now),
    }


def _oldest(conn: sqlite3.Connection, sql: str, now: datetime) -> float | None:
    value = conn.execute(sql).fetchone()[0]
    dt = parse_iso(value)
    return None if dt is None else max(0.0, (now - dt).total_seconds())
