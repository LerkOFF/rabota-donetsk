"""Раздел «Система»: фоновые задачи, опросники источников, очереди, интеграции, диск, версия, хвост журнала.

Каждый блок собирается отдельно и не поднимает исключений: чего нет (воркера, папки копий, git, журнала),
то показывается как «нет данных». Интеграции показываются только как «настроено или нет», значения не читаются
наружу. Строки журнала с токенами и паролями пропускаются.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from rabota import config
from rabota.admin.common import now_utc
from rabota.lifecycle import parse_iso

# состояние -> (подпись, оттенок метки)
STATES: dict[str, tuple[str, str]] = {
    "ok": ("Работает", "ok"),
    "late": ("Опаздывает", "warn"),
    "silent": ("Молчит", "danger"),
    "error": ("Ошибка", "danger"),
    "unknown": ("Нет данных", "mute"),
}
PROBLEM_STATES = ("late", "silent", "error")

EXPIRE_KEY = "job:expire"
EXPIRE_EVERY = 24 * 3600.0
BACKUP_EVERY = 24 * 3600.0

LOG_LINES = 120
LOG_READ_BYTES = 256 * 1024
LOG_FILTER_MAX = 60

# строки журнала с чем-либо похожим на секрет не показываются совсем
_SECRET_LINE = re.compile(
    r"(?i)(token|passw|passwd|secret|api[_-]?key|apikey|authorization|cookie|bearer|session|credential|hash=|sk-[a-z0-9]{8,}|"
    r"bot\d{5,}:|[a-f0-9]{64,}|://[^\s/@:]+:[^\s/@]+@)"
)


_ERROR_LINE = re.compile(r"(?i)error|fail|ошиб|traceback|exception|critical")


# ------------------------------------------------------------------ состояние задач

def classify(age: float | None, threshold: float, error: str | None = None, error_newer: bool = False) -> str:
    """Состояние по возрасту последнего успеха.

    Ошибка последней попытки важнее возраста. Без единого успеха: «нет данных». Опаздывает: старше половины порога,
    молчит: старше порога (порог - четыре интервала опроса).
    """
    if error and error_newer:
        return "error"
    if age is None:
        return "unknown"
    if age > threshold:
        return "silent"
    if age > threshold / 2:
        return "late"
    return "ok"


def _age(value: str | None, now: datetime) -> float | None:
    dt = parse_iso(value)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def worker_block(health: Callable[[sqlite3.Connection], Any] | None, conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Сердцебиение воркера и свежесть источников."""
    out: dict[str, Any] = {"status": "unknown", "age": None, "error": "", "sources": []}
    try:
        from rabota.realtime.health import check_health

        report = health(conn) if health is not None else check_health(conn, now)
        out["status"] = report.worker_status
        out["age"] = report.worker_age_seconds
        thresholds = {s.key: s.threshold_seconds for s in report.sources}
    except Exception as exc:  # noqa: BLE001 - раздел должен открываться, даже если проверка сломана
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    try:
        rows = {r["source"]: r for r in conn.execute("SELECT * FROM ingest_state")}
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rows = {}
    for s in report.sources:
        row = rows.get(s.key)
        last_ok = row["last_ok_at"] if row else None
        updated = row["updated_at"] if row else None
        error = (row["last_error"] if row else None) or ""
        ok_dt, upd_dt = parse_iso(last_ok), parse_iso(updated)
        newer = bool(error) and (ok_dt is None or (upd_dt is not None and upd_dt > ok_dt))
        age = _age(last_ok, now)
        out["sources"].append({
            "key": s.key, "label": s.label, "state": classify(age, thresholds.get(s.key, s.threshold_seconds), error, newer),
            "last_ok": last_ok, "age": age, "attempt": updated, "error": error if newer else "",
            "threshold": s.threshold_seconds,
        })
    return out


def background_jobs(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Задачи вне опросников: ежедневная уборка воркера и ночная копия базы."""
    jobs: list[dict[str, Any]] = []
    last = None
    error = ""
    try:
        row = conn.execute("SELECT * FROM ingest_state WHERE source = ?", (EXPIRE_KEY,)).fetchone()
        if row:
            last, error = row["last_ok_at"], row["last_error"] or ""
    except sqlite3.Error:
        pass
    age = _age(last, now)
    jobs.append({"label": "Уборка старых вакансий", "note": "раз в сутки, воркер", "state": classify(age, EXPIRE_EVERY * 2.5, error, bool(error)),
                 "last": last, "age": age, "error": error})
    backup_at, backup_age = latest_backup(now)
    jobs.append({"label": "Резервная копия базы", "note": "раз в сутки, таймер", "state": classify(backup_age, BACKUP_EVERY * 2.5),
                 "last": backup_at, "age": backup_age, "error": ""})
    return jobs


def latest_backup(now: datetime) -> tuple[str | None, float | None]:
    directory = Path(config.get("BACKUP_DIR", "/var/backups/rabota"))
    try:
        files = [p for p in directory.glob("rabota-*.db*") if p.is_file()]
        if not files:
            return None, None
        newest = max(files, key=lambda p: p.stat().st_mtime)
        stamp = datetime.fromtimestamp(newest.stat().st_mtime, now.tzinfo)
        return stamp.isoformat(), max(0.0, (now - stamp).total_seconds())
    except OSError:
        return None, None


# ------------------------------------------------------------------ очереди

def _count(conn: sqlite3.Connection, sql: str) -> int | None:
    try:
        return int(conn.execute(sql).fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None


def queues(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Что ждёт обработки: (подпись, число, ссылка, считать ли проблемой при ненулевом значении)."""
    rows = [
        ("Вакансии на проверке", _count(conn, "SELECT COUNT(*) FROM vacancies WHERE moderation_status='pending'"), "/admin/moderation", False),
        ("Открытые жалобы на вакансии", _count(conn, "SELECT COUNT(*) FROM complaints WHERE status='new'"), "/admin/complaints", False),
        ("Жалобы на сообщения", _count(conn, "SELECT COUNT(*) FROM reports WHERE status='new'"), "/admin/reports", False),
        ("Запросы по данным в работе", _count(conn, "SELECT COUNT(*) FROM legal_requests WHERE status != 'done'"), "/admin/requests", False),
        ("Запросы по данным просрочены",
         _count(conn, f"SELECT COUNT(*) FROM legal_requests WHERE status IN ('new','in_progress') AND due_at < '{now.isoformat()}'"),
         "/admin/requests?view=overdue", True),
        ("Источники с ошибкой последнего опроса",
         _count(conn, "SELECT COUNT(*) FROM ingest_state WHERE last_error IS NOT NULL AND last_error != '' AND source NOT LIKE 'job:%'"), "", True),
    ]
    outbox = outbox_count()
    if outbox is not None:
        rows.append(("Письма в папке отправки (режим file)", outbox, "", False))
    return [{"label": a, "count": b, "link": c, "bad": d} for a, b, c, d in rows]


def outbox_count() -> int | None:
    """Письма, сложенные в папку вместо отправки (только режим MAIL_BACKEND=file)."""
    if (config.get("MAIL_BACKEND") or "file").strip().lower() != "file":
        return None
    try:
        directory = config.DB_PATH.parent / "outbox"
        return sum(1 for p in directory.iterdir() if p.is_file()) if directory.is_dir() else 0
    except OSError:
        return None


# ------------------------------------------------------------------ интеграции

def _set(name: str) -> bool:
    return bool(config.get(name).strip())


def integrations() -> list[dict[str, Any]]:
    """Только «настроено или нет» и нейтральное описание. Значения ключей и адресов наружу не идут."""
    mail_backend = (config.get("MAIL_BACKEND") or "file").strip().lower()
    llm = config.get("LLM_PROVIDER").strip().lower()
    items = [
        ("ВКонтакте", _set("VK_ACCESS_TOKEN"), "чтение стен пабликов"),
        ("hh.ru", _set("HH_CLIENT_ID") and _set("HH_CLIENT_SECRET"), "открытое API вакансий"),
        ("Telegram, слушатель каналов", _set("TG_API_ID") and _set("TG_API_HASH"), "чтение каналов в реальном времени"),
        ("Прокси для Telegram", bool(config.telegram_proxy()), "доступ к Telegram с сервера"),
        ("Яндекс Метрика", bool(config.metrika_id()), "счётчик посещаемости"),
        ("IndexNow", _set("INDEXNOW_KEY"), "уведомление поисковиков о новых вакансиях"),
        ("Почта", mail_backend == "smtp" and _set("SMTP_HOST"),
         "отправка писем по SMTP" if mail_backend == "smtp" else "письма складываются в папку, наружу не уходят"),
        ("Модель или распознавание афиш", bool(llm), f"режим: {llm}" if llm else "разбор только правилами"),
        ("Уведомления о сбоях в Telegram", _set("ALERT_TG_BOT_TOKEN") and _set("ALERT_TG_CHAT_ID"), "бот для оповещения владельца"),
        ("Подробная проверка /healthz/deep", _set("HEALTH_TOKEN"), "доступ по токену в заголовке"),
    ]
    return [{"name": n, "ok": bool(ok), "what": w} for n, ok, w in items]


# ------------------------------------------------------------------ диск, база, версия

def human_bytes(value: float | None) -> str:
    if value is None:
        return "нет данных"
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{size:.1f} ТБ"


def disk_and_db(db_path: Path | str, conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"total": None, "free": None, "used_share": None, "db_bytes": None, "wal_bytes": None,
                           "vacancies": None, "raw_posts": None, "users": None}
    path = Path(db_path)
    try:
        usage = shutil.disk_usage(path.parent if path.parent.exists() else Path("."))
        out.update(total=usage.total, free=usage.free, used_share=(1 - usage.free / usage.total) if usage.total else None)
    except OSError:
        pass
    try:
        out["db_bytes"] = path.stat().st_size
    except OSError:
        pass
    try:
        out["wal_bytes"] = Path(str(path) + "-wal").stat().st_size
    except OSError:
        pass
    out["vacancies"] = _count(conn, "SELECT COUNT(*) FROM vacancies")
    out["raw_posts"] = _count(conn, "SELECT COUNT(*) FROM raw_posts")
    out["users"] = _count(conn, "SELECT COUNT(*) FROM users")
    for name in ("total", "free", "db_bytes", "wal_bytes"):
        out[name + "_text"] = human_bytes(out[name])
    out["used_percent"] = None if out["used_share"] is None else round(out["used_share"] * 100)
    return out


def version_info() -> dict[str, str]:
    """Коммит кода на сервере (если есть git или файл .git/HEAD), версия Python, режим."""
    info = {"commit": "", "committed_at": "", "python": ".".join(map(str, sys.version_info[:3])), "environment": config.environment()}
    root = config.ROOT
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h|%cI"], capture_output=True, text=True, timeout=3, check=False,
        )
        if out.returncode == 0 and "|" in out.stdout:
            info["commit"], info["committed_at"] = out.stdout.strip().split("|", 1)
            return info
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # git нет, но каталог .git читается
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            head = (root / ".git" / head.split(" ", 1)[1].strip()).read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", head):
            info["commit"] = head[:7]
    except OSError:
        pass
    return info


# ------------------------------------------------------------------ журнал

def log_sources() -> dict[str, str]:
    """Доступные журналы: ключ -> подпись. Пути в интерфейс не уходят, выбирается только ключ."""
    sources: dict[str, str] = {}
    if config.get("LOG_FILE").strip():
        sources["app"] = "Журнал приложения"
    if config.is_production() and shutil.which("journalctl"):
        sources["web"] = "Служба сайта"
        sources["worker"] = "Воркер"
    sources["deploy"] = "Развёртывание"
    return sources


def _journal(unit: str) -> list[str]:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", "400", "-o", "cat", "--no-pager"], capture_output=True, text=True, timeout=4, check=False,
        )
        return out.stdout.splitlines() if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def _file_tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_READ_BYTES))
            data = fh.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def safe_line(line: str) -> str | None:
    """Строка без секретов или None, если её лучше не показывать."""
    from rabota.ops.logs import redact

    text = " ".join(line.split())[:400]
    if not text or _SECRET_LINE.search(redact(text)) or _SECRET_LINE.search(text):
        return None
    return text


def log_tail(source: str, needle: str = "", limit: int = LOG_LINES) -> dict[str, Any]:
    """Последние строки выбранного журнала, новые сверху. needle - подстрока без учёта регистра."""
    sources = log_sources()
    key = source if source in sources else next(iter(sources))
    if key == "app":
        raw = _file_tail(Path(config.get("LOG_FILE").strip()))
    elif key == "deploy":
        raw = _file_tail(Path("/var/log/rabota/deploy.log"))
    else:
        raw = _journal("rabota-web" if key == "web" else "rabota-worker")
    needle = needle.strip()[:LOG_FILTER_MAX].lower()
    lines: list[dict[str, Any]] = []
    hidden = 0
    for line in reversed(raw):
        shown = safe_line(line)
        if shown is None:
            if line.strip():
                hidden += 1
            continue
        if needle and needle not in shown.lower():
            continue
        lines.append({"text": shown, "err": bool(_ERROR_LINE.search(shown))})
        if len(lines) >= limit:
            break
    return {"key": key, "sources": sources, "lines": lines, "hidden": hidden, "needle": needle,
            "available": bool(raw)}


# ------------------------------------------------------------------ сборка страницы

def build(
    conn: sqlite3.Connection, db_path: Path | str, now: datetime | None = None,
    health: Callable[[sqlite3.Connection], Any] | None = None, log_source: str = "", needle: str = "",
) -> dict[str, Any]:
    now = now or now_utc()
    worker = worker_block(health, conn, now)
    jobs = background_jobs(conn, now)
    sources = worker["sources"]
    problems = sum(1 for s in sources if s["state"] in PROBLEM_STATES) + sum(1 for j in jobs if j["state"] in PROBLEM_STATES)
    if worker["status"] in ("stale", "missing"):
        problems += 1
    return {
        "worker": worker, "jobs": jobs, "sources": sources, "problems": problems, "queues": queues(conn, now),
        "integrations": integrations(), "disk": disk_and_db(db_path, conn), "version": version_info(),
        "log": log_tail(log_source, needle), "states": STATES,
    }
