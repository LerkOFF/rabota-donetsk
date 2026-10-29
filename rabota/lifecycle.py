"""Срок жизни вакансии: через TTL дней после последней публикации она скрывается, затем удаляется.

Отсчёт идёт от `last_seen_at`. Когда та же вакансия того же работодателя встречается снова
(в любом источнике), склейка дублей переносит более позднюю дату в главную вакансию и отсчёт начинается заново.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from rabota import config


def parse_iso(value: str | None) -> datetime | None:
    """ISO-строка (с Z, смещением или без) в aware-datetime UTC. Мусор даёт None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_utc_iso(value: str | None) -> str | None:
    """Канонический вид 2026-10-29T12:00:00+00:00: строки сравниваются как текст."""
    dt = parse_iso(value)
    return dt.replace(microsecond=0).isoformat() if dt else None


def later_of(a: str | None, b: str | None) -> str | None:
    da, db = parse_iso(a), parse_iso(b)
    if da is None:
        return to_utc_iso(b)
    if db is None:
        return to_utc_iso(a)
    return to_utc_iso((max(da, db)).isoformat())


def is_stale(last_seen_at: str | None, ttl_days: int | None = None, now: datetime | None = None) -> bool:
    dt = parse_iso(last_seen_at)
    if dt is None:
        return False
    ttl = config.VACANCY_TTL_DAYS if ttl_days is None else ttl_days
    return dt < (now or datetime.now(UTC)) - timedelta(days=ttl)


def _cutoff(days: int, now: datetime | None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(days=days)).replace(microsecond=0).isoformat()


def expire(conn: sqlite3.Connection, ttl_days: int | None = None, now: datetime | None = None) -> int:
    """Скрыть вакансии, которых не видели дольше TTL. Возвращает число скрытых."""
    ttl = config.VACANCY_TTL_DAYS if ttl_days is None else ttl_days
    cur = conn.execute(
        "UPDATE vacancies SET status = 'expired' WHERE status = 'active' "
        "AND COALESCE(last_seen_at, published_at) IS NOT NULL AND COALESCE(last_seen_at, published_at) < ?",
        (_cutoff(ttl, now),),
    )
    conn.commit()
    return cur.rowcount


def purge(
    conn: sqlite3.Connection,
    ttl_days: int | None = None,
    keep_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """Физически удалить истёкшие вакансии, которые лежат скрытыми дольше keep_days, вместе с дублями,
    жалобами и исходными постами. Возвращает число удалённых вакансий (главных и дублей)."""
    ttl = config.VACANCY_TTL_DAYS if ttl_days is None else ttl_days
    keep = config.EXPIRED_KEEP_DAYS if keep_days is None else keep_days
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM vacancies WHERE status = 'expired' AND duplicate_of IS NULL "
            "AND COALESCE(last_seen_at, published_at) < ?",
            (_cutoff(ttl + keep, now),),
        )
    ]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, source, external_id FROM vacancies WHERE id IN ({marks}) OR duplicate_of IN ({marks})", ids + ids
    ).fetchall()
    all_ids = [r["id"] for r in rows]
    m2 = ",".join("?" * len(all_ids))
    conn.execute(f"DELETE FROM complaints WHERE vacancy_id IN ({m2})", all_ids)
    conn.execute(f"DELETE FROM vacancies WHERE id IN ({m2})", all_ids)
    for r in rows:
        conn.execute("DELETE FROM raw_posts WHERE source = ? AND external_id = ?", (r["source"], r["external_id"]))
    conn.commit()
    return len(all_ids)
