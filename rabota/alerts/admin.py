"""Блок «Подписки и рассылка» для раздела «Система»: цифры, включённость и состояние фоновых задач. Сбои не поднимаются."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from rabota.alerts import conf, jobs, service
from rabota.lifecycle import parse_iso
from rabota.telegram_bot import runner

JOBS = (
    (jobs.KEY_EMAIL, "Рассылка писем", "каждые 5 минут"),
    (jobs.KEY_TELEGRAM, "Доставка в Telegram", "каждые 2 минуты"),
    (jobs.KEY_CHANNEL, "Автопостинг в канал", "каждые 10 минут"),
    (runner.POLL_KEY, "Приём команд бота", "длинный опрос"),
)


def _job_rows(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    out = []
    for key, label, note in JOBS:
        try:
            row = conn.execute("SELECT * FROM ingest_state WHERE source = ?", (key,)).fetchone()
        except sqlite3.Error:
            row = None
        ok, upd = parse_iso(row["last_ok_at"]) if row else None, parse_iso(row["updated_at"]) if row else None
        error = (row["last_error"] if row else "") or ""
        failing = bool(error) and (ok is None or (upd is not None and upd > ok))
        out.append({
            "label": label, "note": note, "last_ok": row["last_ok_at"] if row else None,
            "age": max(0.0, (now - ok).total_seconds()) if ok else None, "error": error if failing else "",
            "state": "error" if failing else ("ok" if ok else "unknown"),
        })
    return out


def block(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    cfg = conf.TelegramConfig.from_env()
    try:
        stats = service.stats(conn, now)
    except sqlite3.Error:
        stats = {}
    try:
        email_on = bool(conf.setting(conn, "alerts_email_enabled"))
        bot_on = bool(conf.setting(conn, "tg_bot_enabled"))
        channel_on = bool(conf.setting(conn, "channel_enabled"))
    except Exception:  # noqa: BLE001 - блок не должен ронять страницу
        email_on = bot_on = channel_on = True
    flags = [
        ("Рассылка писем", conf.mail_enabled() and email_on, "почта настроена" if conf.mail_enabled() else "почта не настроена: письма не уходят"),
        ("Бот Telegram", cfg.bot_ready and bot_on, "токен задан" if cfg.bot_ready else "нет TG_BOT_TOKEN"),
        ("Автопостинг в канал", cfg.channel_ready and channel_on, "канал задан" if cfg.channel_ready else "нет TG_CHANNEL_ID"),
    ]
    return {"stats": stats, "flags": [{"name": n, "on": bool(on), "note": note} for n, on, note in flags],
            "jobs": _job_rows(conn, now)}
