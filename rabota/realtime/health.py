"""Здоровье приёма: свежесть по каждому источнику и сердцебиение воркера.

    python -m rabota health [--json]

Код возврата 0, если всё свежее, и 1, если что-то просрочено (годится для мониторинга и cron).
Порог на источник: четыре интервала опроса, но не меньше десяти минут.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from rabota import config
from rabota.lifecycle import parse_iso
from rabota.realtime import state
from rabota.realtime.pollers import POLLERS
from rabota.realtime.state import SourceState

HEARTBEAT_EVERY = 60.0        # секунд: как часто воркер пишет сердцебиение
HEARTBEAT_MAX_AGE = 180.0     # старше этого воркер считается остановленным
MIN_THRESHOLD = 600.0
THRESHOLD_FACTOR = 4


def heartbeat_path() -> Path:
    return Path(config.get("HEARTBEAT_PATH") or config.DB_PATH.parent / "worker.heartbeat")


def threshold_for(interval: float) -> float:
    return max(THRESHOLD_FACTOR * interval, MIN_THRESHOLD)


def expected_sources(env: Mapping[str, str] | None = None) -> dict[str, tuple[str, float]]:
    """Источники, которые воркер должен опрашивать в этой настройке: ключ -> (название, порог в секундах)."""
    from rabota.collectors import vk
    from rabota.realtime.telegram_listener import TelegramNotConfigured, TelegramSettings
    from rabota.realtime.vk_poller import VK_INTERVAL

    env = os.environ if env is None else env
    out: dict[str, tuple[str, float]] = {}
    if (env.get(vk.TOKEN_ENV) or "").strip():
        for src in vk.SOURCES:
            out[state.source_key("vk", src.domain)] = (f"VK {src.domain}", threshold_for(VK_INTERVAL))
    for spec in POLLERS:
        out[spec.key] = (spec.label, threshold_for(spec.interval))
    try:
        tg = TelegramSettings.from_env(env)
    except TelegramNotConfigured:
        tg = None
    if tg is not None:
        for channel in tg.channels:
            out[state.source_key("telegram", channel)] = (f"Telegram {channel}", threshold_for(tg.sweep_seconds))
    return out


@dataclass
class SourceHealth:
    key: str
    label: str
    status: str                  # ok, stale, never
    age_seconds: float | None
    threshold_seconds: float
    last_error: str | None

    @property
    def problem(self) -> bool:
        return self.status != "ok"


@dataclass
class HealthReport:
    sources: list[SourceHealth]
    worker_status: str           # ok, stale, missing
    worker_age_seconds: float | None

    @property
    def ok(self) -> bool:
        return self.worker_status == "ok" and not any(s.problem for s in self.sources)


def read_heartbeat(path: Path) -> datetime | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        raw = json.loads(raw).get("at", "")
    except (ValueError, AttributeError):
        pass
    return parse_iso(raw)


def write_heartbeat(path: Path, now: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"at": state.now_iso(now), "pid": os.getpid()}), encoding="utf-8")
    tmp.replace(path)


def check_health(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    expected: dict[str, tuple[str, float]] | None = None,
    heartbeat: Path | None = None,
) -> HealthReport:
    now = now or datetime.now(UTC)
    expected = expected_sources() if expected is None else expected
    states: dict[str, SourceState] = {s.source: s for s in state.all_states(conn)}
    rows: list[SourceHealth] = []
    for key, (label, threshold) in sorted(expected.items()):
        st = states.get(key)
        ok_at = parse_iso(st.last_ok_at) if st else None
        if ok_at is None:
            rows.append(SourceHealth(key, label, "never", None, threshold, st.last_error if st else None))
            continue
        age = max(0.0, (now - ok_at).total_seconds())
        rows.append(SourceHealth(key, label, "ok" if age <= threshold else "stale", age, threshold, st.last_error))
    beat = read_heartbeat(heartbeat or heartbeat_path())
    if beat is None:
        worker, worker_age = "missing", None
    else:
        worker_age = max(0.0, (now - beat).total_seconds())
        worker = "ok" if worker_age <= HEARTBEAT_MAX_AGE else "stale"
    return HealthReport(rows, worker, worker_age)


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "никогда"
    s = int(seconds)
    if s < 90:
        return f"{s} с назад"
    if s < 5400:
        return f"{round(s / 60)} мин назад"
    if s < 2 * 86400:
        return f"{s // 3600} ч {s % 3600 // 60} мин назад"
    return f"{s // 86400} дн назад"


_STATUS_RU = {"ok": "норма", "stale": "ПРОСРОЧЕН", "never": "НЕТ ДАННЫХ"}


def format_report(report: HealthReport) -> str:
    lines = []
    worker = {
        "ok": f"воркер работает (сердцебиение {human_age(report.worker_age_seconds)})",
        "stale": f"ВОРКЕР МОЛЧИТ: последнее сердцебиение {human_age(report.worker_age_seconds)}",
        "missing": "ВОРКЕР НЕ ЗАПУЩЕН: файла сердцебиения нет",
    }[report.worker_status]
    lines.append(worker)
    for s in report.sources:
        limit = f"порог {round(s.threshold_seconds / 60)} мин"
        line = f"{_STATUS_RU[s.status]:<11} {s.label}: последний успех {human_age(s.age_seconds)}, {limit}"
        if s.last_error:
            line += f"; последняя ошибка: {s.last_error}"
        lines.append(line)
    bad = sum(1 for s in report.sources if s.problem) + (report.worker_status != "ok")
    lines.append("Итог: всё свежее." if report.ok else f"Итог: проблем {bad}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from rabota.realtime.ingest import open_db

    as_json = bool(argv and "--json" in argv)
    conn = open_db(config.DB_PATH)
    try:
        report = check_health(conn)
    finally:
        conn.close()
    if as_json:
        print(json.dumps({"ok": report.ok, "worker": report.worker_status,
                          "sources": [asdict(s) for s in report.sources]}, ensure_ascii=False, indent=1))
    else:
        print(format_report(report))
    return 0 if report.ok else 1
