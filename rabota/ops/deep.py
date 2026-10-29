"""Глубокая проверка /healthz/deep: база, воркер, свежесть источников.

Без токена отдаётся только то, что можно показать любому: общий статус и "ok/fail" по частям.
С заголовком X-Health-Token (значение HEALTH_TOKEN в .env) добавляются подробности: названия источников,
возраст, тексты ошибок и счётчики. Код ответа 200 при "ok" и "degraded", 503 при "fail".

    ok        база читается, воркер жив, все группы источников в порядке
    degraded  часть источников просрочена, но каждая группа в целом работает
    fail      база недоступна, воркер молчит или целая группа источников просрочена
"""
from __future__ import annotations

import hmac
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rabota import config
from rabota.ops.logs import counters, redact
from rabota.realtime import health as rt_health

CACHE_SECONDS = 15.0
SOURCE_GROUPS = {"telegram": "Telegram", "vk": "VK", "poll": "Открытые базы"}


def group_of(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else key


def group_state(total: int, bad: int) -> str:
    """ok, degraded или down. Группа падает, когда просрочены все источники или больше половины (если их 4 и больше)."""
    if bad == 0:
        return "ok"
    if bad == total or (total >= 4 and bad * 2 > total):
        return "down"
    return "degraded"


def check_db(db_path: Path | str) -> bool:
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            conn.execute("SELECT 1 FROM vacancies LIMIT 1").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def build_report(
    db_path: Path | str,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    heartbeat: Path | None = None,
) -> dict[str, Any]:
    """Полный отчёт со всеми подробностями. Для публичного ответа его урезает public_view."""
    now = now or datetime.now(UTC)
    heartbeat = heartbeat or Path(config.get("HEARTBEAT_PATH") or Path(db_path).parent / "worker.heartbeat")
    db_ok = check_db(db_path)
    worker_status, worker_age = "missing", None
    groups: dict[str, list[rt_health.SourceHealth]] = defaultdict(list)
    if db_ok:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            expected = rt_health.expected_sources(env)
            report = rt_health.check_health(conn, now, expected=expected, heartbeat=heartbeat)
            worker_status, worker_age = report.worker_status, report.worker_age_seconds
            for src in report.sources:
                groups[group_of(src.key)].append(src)
        except sqlite3.Error:
            db_ok = False
        finally:
            conn.close()
    group_info: dict[str, dict[str, Any]] = {}
    for name, items in sorted(groups.items()):
        bad = [s for s in items if s.problem]
        group_info[name] = {
            "state": group_state(len(items), len(bad)),
            "total": len(items),
            "bad": len(bad),
            "problems": [
                {"key": s.key, "label": s.label, "status": s.status, "age_seconds": s.age_seconds,
                 "last_error": redact(s.last_error) if s.last_error else None}
                for s in bad
            ],
        }
    if not db_ok or worker_status != "ok" or any(g["state"] == "down" for g in group_info.values()):
        status = "fail"
    elif any(g["state"] != "ok" for g in group_info.values()):
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "db": "ok" if db_ok else "fail",
        "worker": worker_status,
        "worker_age_seconds": worker_age,
        "groups": group_info,
        "checked_at": now.replace(microsecond=0).isoformat(),
    }


def public_view(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "db": report["db"],
        "worker": "ok" if report["worker"] == "ok" else "fail",
        "sources": {name: info["state"] for name, info in report["groups"].items()},
    }


def detailed_view(report: dict[str, Any]) -> dict[str, Any]:
    out = dict(report)
    out["counters"] = counters.snapshot()
    out["uptime_seconds"] = int((datetime.now(UTC) - counters.started_at).total_seconds())
    return out


def token_ok(request: Request, expected: str) -> bool:
    given = request.headers.get("x-health-token", "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def install(app: FastAPI, db_path: Path | str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
    """Добавить GET /healthz/deep. Результат кешируется на 15 секунд: проверка не нагружает базу."""
    path = Path(db_path) if db_path else config.DB_PATH
    cache: dict[str, Any] = {"at": -1e9, "report": None}
    lock = threading.Lock()

    @app.get("/healthz/deep", include_in_schema=False)
    def healthz_deep(request: Request) -> JSONResponse:
        with lock:
            if cache["report"] is None or clock() - cache["at"] > CACHE_SECONDS:
                cache["report"] = build_report(path)
                cache["at"] = clock()
            report = cache["report"]
        detailed = token_ok(request, config.get("HEALTH_TOKEN"))
        body = detailed_view(report) if detailed else public_view(report)
        return JSONResponse(
            body,
            status_code=503 if report["status"] == "fail" else 200,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )
