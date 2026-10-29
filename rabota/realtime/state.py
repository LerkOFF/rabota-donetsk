"""Состояние приёма: таблица ingest_state (на источник последний id, время успеха и ошибка).

Ключ источника: `<тип>:<имя>`, например `telegram:rabota_donetskv`, `vk:dnr.work`, `poll:trudvsem`.
Ключ в нижнем регистре не приводится: за единый вид отвечает вызывающий (см. `source_key`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

ERROR_MAX_CHARS = 300


def source_key(kind: str, name: str) -> str:
    """telegram + Rabota_Donetskv -> telegram:rabota_donetskv (имена каналов и пабликов без учёта регистра)."""
    return f"{kind}:{name.strip().lower()}"


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class SourceState:
    source: str
    last_id: int | None
    updated_at: str | None
    last_ok_at: str | None
    last_error: str | None


def _row(r: sqlite3.Row) -> SourceState:
    return SourceState(r["source"], r["last_id"], r["updated_at"], r["last_ok_at"], r["last_error"])


def get_state(conn: sqlite3.Connection, source: str) -> SourceState | None:
    r = conn.execute("SELECT * FROM ingest_state WHERE source = ?", (source,)).fetchone()
    return _row(r) if r else None


def all_states(conn: sqlite3.Connection) -> list[SourceState]:
    return [_row(r) for r in conn.execute("SELECT * FROM ingest_state ORDER BY source")]


def get_last_id(conn: sqlite3.Connection, source: str) -> int | None:
    st = get_state(conn, source)
    return st.last_id if st else None


def mark_ok(
    conn: sqlite3.Connection, source: str, last_id: int | None = None, now: datetime | None = None
) -> None:
    """Успешный опрос или сообщение. last_id только растёт: меньшее значение его не откатывает."""
    ts = now_iso(now)
    conn.execute(
        "INSERT INTO ingest_state(source, last_id, updated_at, last_ok_at, last_error) VALUES (?,?,?,?,NULL) "
        "ON CONFLICT(source) DO UPDATE SET "
        "last_id = CASE WHEN excluded.last_id IS NULL THEN last_id "
        "WHEN last_id IS NULL OR excluded.last_id > last_id THEN excluded.last_id ELSE last_id END, "
        "updated_at = excluded.updated_at, last_ok_at = excluded.last_ok_at, last_error = NULL",
        (source, last_id, ts, ts),
    )
    conn.commit()


def mark_error(conn: sqlite3.Connection, source: str, error: str, now: datetime | None = None) -> None:
    """Ошибка опроса. last_ok_at и last_id не трогаются: по ним видно, как давно было хорошо."""
    ts = now_iso(now)
    text = " ".join(str(error).split())[:ERROR_MAX_CHARS]
    conn.execute(
        "INSERT INTO ingest_state(source, last_id, updated_at, last_ok_at, last_error) VALUES (?,NULL,?,NULL,?) "
        "ON CONFLICT(source) DO UPDATE SET updated_at = excluded.updated_at, last_error = excluded.last_error",
        (source, ts, text),
    )
    conn.commit()


def mark_empty(conn: sqlite3.Connection, source: str, note: str, now: datetime | None = None) -> None:
    """Опрос прошёл, но источник ничего не отдал: это не успех (возможна блокировка), last_ok_at не обновляется."""
    mark_error(conn, source, note, now)
