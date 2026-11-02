"""Показатели работодателя для страницы компании и карточки вакансии: сколько откликов получено и как быстро он отвечает.

Показатель виден, только если данных хватает: меньше MIN_RESPONSES откликов или MIN_ANSWERED ответов, и он не
показывается вовсе (лучше пусто, чем число из одного отклика). Ответом считается любое решение работодателя по
отклику: просмотрел, пригласил, отказал (поле `status_changed_at`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from rabota.lifecycle import parse_iso
from rabota.web import format as fmt

MIN_RESPONSES = 1
MIN_ANSWERED = 3
WINDOW_DAYS = 90
SAMPLE = 200


@dataclass(frozen=True)
class Stats:
    responses: int = 0
    answered: int = 0
    avg_seconds: int | None = None

    @property
    def show_responses(self) -> bool:
        return self.responses >= MIN_RESPONSES

    @property
    def show_reply_time(self) -> bool:
        return self.avg_seconds is not None and self.answered >= MIN_ANSWERED

    @property
    def reply_text(self) -> str:
        return duration_text(self.avg_seconds) if self.show_reply_time else ""

    @property
    def visible(self) -> bool:
        return self.show_responses or self.show_reply_time


def duration_text(seconds: float | None) -> str:
    """"в течение часа", "около 3 часов", "около 2 дней": грубо и по-человечески."""
    if seconds is None or seconds < 0:
        return ""
    minutes = seconds / 60
    if minutes < 60:
        return "в течение часа"
    hours = round(minutes / 60)
    if hours < 24:
        return f"около {hours} {fmt.plural(hours, 'часа', 'часов', 'часов')}"
    days = round(hours / 24)
    return f"около {days} {fmt.plural(days, 'дня', 'дней', 'дней')}"


def average_seconds(pairs: list[tuple[str, str]]) -> int | None:
    """Среднее время между откликом и ответом по парам ISO-строк. Пары с обратным порядком пропускаются."""
    deltas: list[float] = []
    for created, answered in pairs:
        a, b = parse_iso(created), parse_iso(answered)
        if a is None or b is None or b < a:
            continue
        deltas.append((b - a).total_seconds())
    if not deltas:
        return None
    deltas.sort()
    # медиана устойчивее среднего: один отклик, на который ответили через месяц, картину не портит
    mid = len(deltas) // 2
    value = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2
    return int(value)


def _table_ok(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    return {"created_at", "status_changed_at", "vacancy_id", "status"} <= cols


def employer_stats(conn: sqlite3.Connection, company_id: int, now=None) -> Stats:
    """Показатели по вакансиям компании за последние WINDOW_DAYS дней. Отозванные отклики не считаются."""
    if not company_id or not _table_ok(conn):
        return Stats()
    since = None
    if now is not None:
        since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    where = "v.company_id = ? AND a.withdrawn_at IS NULL"
    args: list = [company_id]
    if since:
        where += " AND substr(a.created_at, 1, 10) >= ?"
        args.append(since)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE {where}", args
    ).fetchone()[0])
    rows = conn.execute(
        f"SELECT a.created_at, a.status_changed_at FROM applications a JOIN vacancies v ON v.id = a.vacancy_id"
        f" WHERE {where} AND a.status != 'new' AND a.status_changed_at IS NOT NULL ORDER BY a.id DESC LIMIT {SAMPLE}",
        args,
    ).fetchall()
    pairs = [(r["created_at"], r["status_changed_at"]) for r in rows]
    return Stats(total, len(pairs), average_seconds(pairs))
