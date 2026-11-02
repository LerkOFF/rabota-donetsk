"""Что вызывают конвейер, воркер и командная строка: привязка новых вакансий к карточкам и уборка архива."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from rabota.employers_auto import archive, cards

log = logging.getLogger("rabota.employers_auto")

PERIOD = 30 * 60.0  # как часто воркер подбирает карточки для новых вакансий, секунд


def attach_new(conn: sqlite3.Connection, limit: int = 2000, now: datetime | None = None) -> dict[str, Any]:
    """Привязать новые вакансии к карточкам работодателей. Сбой не роняет приём вакансий."""
    try:
        if not cards.enabled(conn):
            return {"выключено": 1}
        report = cards.attach_pending(conn, limit, now)
    except sqlite3.OperationalError:
        raise
    except Exception:
        log.warning("не удалось привязать вакансии к карточкам", exc_info=True)
        return {"ошибка": 1}
    return {"привязано": report.linked, "создано": report.created, "пропущено": sum(report.skipped.values())}


def employers_job(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Периодическая задача воркера: подобрать карточки пропущенным вакансиям."""
    return attach_new(conn, 5000, now)


def maintenance(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    """Ежедневная уборка архива (вызывается из задачи expire воркера)."""
    return archive.run_all(conn, now)
