"""Пульс: плитки с числом за период, сравнением с прошлым таким же периодом и спарклайном по дням.

Границы периодов считает код по местному времени Донецка (UTC+3), а не `NOW()` базы: «сегодня» начинается в местную
полночь. В базе время хранится в UTC в двух видах (`2026-10-29 11:30:00` и `2026-10-29T11:30:00+00:00`), поэтому
сравнение идёт по первым 19 знакам с заменой `T` на пробел, а день и час для графика получаются сдвигом на местное время.

Пустое значение (источник недоступен или таких данных в базе ещё никогда не было) показывается как «нет данных», а не как ноль.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rabota import config
from rabota.admin.common import now_utc
from rabota.web import format as fmt

LOCAL_UTC_OFFSET = timedelta(hours=3)  # Донецк, московское время; то же смещение, что у шаблонов админки

PERIODS: tuple[tuple[str, str], ...] = (
    ("today", "Сегодня"), ("yesterday", "Вчера"), ("7d", "7 дней"), ("30d", "30 дней"), ("month", "Месяц"), ("all", "Всё"),
)
DEFAULT_PERIOD = "7d"
WEEKLY_AFTER_DAYS = 62  # длиннее: точки спарклайна по неделям

_FMT = "%Y-%m-%d %H:%M:%S"
_OFFSET_SQL = f"'+{int(LOCAL_UTC_OFFSET.total_seconds() // 3600)} hours'"


# ------------------------------------------------------------------ периоды

@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: datetime                 # местное время, без tzinfo
    end: datetime                   # местное время, не включается
    prev_start: datetime | None
    prev_end: datetime | None
    prev_label: str
    step: str                       # hour | day | week

    @property
    def has_prev(self) -> bool:
        return self.prev_start is not None and self.prev_end is not None and self.prev_end > self.prev_start


def local_now(now: datetime | None = None) -> datetime:
    """Текущее местное время (Донецк) без tzinfo. Наивное время считается UTC."""
    now = now or now_utc()
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return now.replace(tzinfo=None) + LOCAL_UTC_OFFSET


def to_utc_text(local: datetime) -> str:
    return (local - LOCAL_UTC_OFFSET).strftime(_FMT)


def normalize(key: str | None) -> str:
    return key if key in dict(PERIODS) else DEFAULT_PERIOD


def _prev_month_start(month_start: datetime) -> datetime:
    last = month_start - timedelta(days=1)
    return last.replace(day=1)


def build_period(key: str | None, now: datetime | None = None, first: datetime | None = None) -> Period:
    """Границы периода. `first` - местное время самой ранней записи (нужно только для «Всё»)."""
    key = normalize(key)
    cur = local_now(now).replace(microsecond=0)
    midnight = cur.replace(hour=0, minute=0, second=0)
    label = dict(PERIODS)[key]
    if key == "today":
        elapsed = cur - midnight
        prev_start = midnight - timedelta(days=1)
        return Period(key, label, midnight, cur, prev_start, prev_start + elapsed, "вчера к этому же времени", "hour")
    if key == "yesterday":
        start = midnight - timedelta(days=1)
        return Period(key, label, start, midnight, start - timedelta(days=1), start, "позавчера", "hour")
    if key in ("7d", "30d"):
        days = 7 if key == "7d" else 30
        start = midnight - timedelta(days=days - 1)
        length = cur - start
        return Period(key, label, start, cur, start - length, start, f"предыдущие {days} дней", "day")
    if key == "month":
        start = midnight.replace(day=1)
        prev_start = _prev_month_start(start)
        prev_end = min(prev_start + (cur - start), start)
        return Period(key, label, start, cur, prev_start, prev_end, "прошлый месяц за те же дни", "day")
    start = (first or midnight).replace(hour=0, minute=0, second=0, microsecond=0)
    start = min(start, midnight)
    step = "week" if (cur - start).days > WEEKLY_AFTER_DAYS else "day"
    return Period(key, label, start, cur, None, None, "", step)


def buckets(period: Period) -> list[tuple[str, int]]:
    """Точки графика: (ключ дня или часа в местном времени, номер точки)."""
    out: list[tuple[str, int]] = []
    if period.step == "hour":
        t = period.start
        while t < period.end:
            out.append((t.strftime("%Y-%m-%d %H"), len(out)))
            t += timedelta(hours=1)
        return out
    d = period.start.date()
    last = (period.end - timedelta(seconds=1)).date()
    n = 0
    while d <= last:
        index = n // 7 if period.step == "week" else n
        out.append((d.isoformat(), index))
        d += timedelta(days=1)
        n += 1
    return out


# ------------------------------------------------------------------ метрики

def _ts(column: str) -> str:
    return f"substr(replace({column}, 'T', ' '), 1, 19)"


@dataclass(frozen=True)
class Source:
    """Откуда считать: таблица, выражение времени события, условие и разбивка."""

    table: str
    ts: str
    where: str = "1=1"
    group: str = "''"
    names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    hint: str
    href: str
    sources: tuple[Source, ...]
    lower_better: bool | None = False   # True: меньше лучше, None: без оценки (цвет нейтральный)


_SOURCE_GROUP = "CASE WHEN origin = 'direct' THEN 'site' ELSE source_type END"
_HIDE_GROUP = (
    "CASE action WHEN 'vacancy.autohide' THEN 'автоскрытие по жалобам' WHEN 'vacancy.stoplist_hide' THEN 'по стоп-листу' "
    "ELSE 'вручную' END"
)
_ROLE_NAMES = {"job_seeker": "соискатели", "employer": "работодатели", "admin": "администраторы"}


def metrics() -> tuple[Metric, ...]:
    ttl = int(config.VACANCY_TTL_DAYS)
    life = f"datetime({_ts('COALESCE(last_seen_at, published_at, created_at)')}, '+{ttl} days')"
    return (
        Metric("new", "Новые вакансии", "добавлено в базу, по источникам", "/admin/moderation?view=all", (
            Source("vacancies", _ts("created_at"), "kind = 'vacancy' AND duplicate_of IS NULL", _SOURCE_GROUP, fmt.SOURCE_LABELS),
        ), lower_better=False),
        Metric("moderation", "Поступило на модерацию", "вакансии, размещённые через сайт", "/admin/moderation", (
            Source("vacancies", _ts("created_at"), "origin = 'direct'"),
        ), lower_better=None),
        Metric("hidden", "Скрыто", "вакансии, снятые из выдачи", "/admin/audit", (
            Source("audit_log", _ts("at"), "action IN ('vacancy.hide', 'vacancy.autohide', 'vacancy.stoplist_hide')", _HIDE_GROUP),
        ), lower_better=True),
        Metric("expired", "Просрочено", f"срок жизни ({ttl} дн.) истёк, вакансию не обновляли", "/admin/moderation?view=all", (
            Source("vacancies", life, "kind = 'vacancy' AND duplicate_of IS NULL AND moderation_status = 'approved' "
                   "AND status IN ('active', 'expired')"),
        ), lower_better=None),
        Metric("registrations", "Регистрации", "новые аккаунты", "/admin", (
            Source("users", _ts("created_at"), "1=1", "role", _ROLE_NAMES),
        ), lower_better=False),
        Metric("responses", "Отклики", "отклики соискателей на вакансии", "/admin", (
            Source("applications", _ts("created_at")),
        ), lower_better=False),
        Metric("complaints", "Жалобы", "на вакансии и на сообщения", "/admin/complaints", (
            Source("complaints", _ts("created_at"), "1=1", "'на вакансии'"),
            Source("reports", _ts("created_at"), "1=1", "'на сообщения и людей'"),
        ), lower_better=True),
    )


def _fetch(conn: sqlite3.Connection, src: Source, start_utc: str, end_utc: str, gran: str) -> dict[tuple[str, str], int]:
    fmt_sql = "%Y-%m-%d %H" if gran == "hour" else "%Y-%m-%d"
    sql = (
        f"SELECT {src.group} AS g, strftime('{fmt_sql}', {src.ts}, {_OFFSET_SQL}) AS b, COUNT(*) AS n "
        f"FROM {src.table} WHERE ({src.where}) AND {src.ts} >= ? AND {src.ts} < ? GROUP BY g, b"
    )
    out: dict[tuple[str, str], int] = {}
    for row in conn.execute(sql, (start_utc, end_utc)):
        if row["b"] is not None:
            out[(str(row["g"]), row["b"])] = int(row["n"])
    return out


def _ever(conn: sqlite3.Connection, src: Source) -> bool:
    return conn.execute(f"SELECT 1 FROM {src.table} WHERE ({src.where}) LIMIT 1").fetchone() is not None


# ------------------------------------------------------------------ сравнение и график

def delta(cur: int | None, prev: int | None, lower_better: bool | None) -> dict[str, Any]:
    """Сравнение с прошлым периодом: процент, цвет (good, bad, flat, none) и текст."""
    if cur is None or prev is None:
        return {"pct": None, "tone": "none", "text": "нет сравнения", "arrow": ""}
    if cur == prev:
        return {"pct": 0.0, "tone": "flat", "text": "без изменений", "arrow": "="}
    diff = cur - prev
    up = diff > 0
    if lower_better is None:
        tone = "flat"
    else:
        tone = "good" if (up != lower_better) else "bad"
    if prev == 0:
        return {"pct": None, "tone": tone, "text": f"+{diff}, раньше не было", "arrow": "↑"}
    pct = diff / prev * 100
    sign = "+" if up else "−"
    return {"pct": pct, "tone": tone, "text": f"{sign}{abs(round(pct)):d}%", "arrow": "↑" if up else "↓"}


SPARK_W, SPARK_H, SPARK_PAD = 120, 32, 3


def spark(values: Sequence[int]) -> dict[str, Any] | None:
    """Геометрия спарклайна для инлайн SVG. Меньше двух точек: графика нет."""
    n = len(values)
    if n < 2:
        return None
    top = max(values) or 1
    pts = []
    for i, v in enumerate(values):
        x = round(i / (n - 1) * SPARK_W, 1)
        y = round(SPARK_H - SPARK_PAD - v / top * (SPARK_H - 2 * SPARK_PAD), 1)
        pts.append((x, y))
    line = "M" + " L".join(f"{x} {y}" for x, y in pts)
    return {"line": line, "area": f"{line} L{SPARK_W} {SPARK_H} L0 {SPARK_H} Z", "x": pts[-1][0], "y": pts[-1][1],
            "w": SPARK_W, "h": SPARK_H, "max": max(values), "total": sum(values)}


def _label(period: Period, key: str) -> str:
    if period.step == "hour":
        return f"{key[-2:]}:00"
    y, m, d = key.split("-")
    return f"{d}.{m}.{y}"


def _tile(conn: sqlite3.Connection, metric: Metric, period: Period, points: list[tuple[str, int]]) -> dict[str, Any]:
    gran = "hour" if period.step == "hour" else "day"
    size = (max(i for _, i in points) + 1) if points else 0
    series = [0] * size
    labels = [""] * size
    for key, i in points:
        if not labels[i]:
            labels[i] = _label(period, key)
    groups: dict[str, int] = defaultdict(int)
    prev_total: int | None = None
    try:
        start_utc, end_utc = to_utc_text(period.start), to_utc_text(period.end)
        index = {key: i for key, i in points}
        for src in metric.sources:
            for (group, bucket), n in _fetch(conn, src, start_utc, end_utc, gran).items():
                if bucket in index:
                    series[index[bucket]] += n
                name = src.names.get(group, group) if group else ""
                if group:
                    groups[name or "без источника"] += n
        total: int | None = sum(series)
        if period.has_prev:
            p_start, p_end = to_utc_text(period.prev_start), to_utc_text(period.prev_end)  # type: ignore[arg-type]
            prev_total = sum(sum(_fetch(conn, s, p_start, p_end, gran).values()) for s in metric.sources)
        if total == 0 and not any(_ever(conn, s) for s in metric.sources):
            total, prev_total = None, None  # таких данных не было никогда: это не ноль, а отсутствие данных
    except sqlite3.Error:
        total, prev_total, series = None, None, []
    cmp = delta(total, prev_total, metric.lower_better)
    breakdown = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "key": metric.key, "title": metric.title, "hint": metric.hint, "href": metric.href,
        "value": total, "prev": prev_total, "delta": cmp, "lower_better": metric.lower_better,
        "series": series, "labels": labels, "spark": spark(series) if total is not None else None,
        "breakdown": [(name, n) for name, n in breakdown if n][:5], "note": "",
    }


def _queue_note(conn: sqlite3.Connection) -> str:
    try:
        n = int(conn.execute("SELECT COUNT(*) FROM vacancies WHERE moderation_status = 'pending'").fetchone()[0])
    except sqlite3.Error:
        return ""
    return f"сейчас в очереди: {n}"


def first_record(conn: sqlite3.Connection) -> datetime | None:
    """Местное время самой ранней записи: начало периода «Всё»."""
    best: str | None = None
    for table, col in (("vacancies", "created_at"), ("users", "created_at"), ("applications", "created_at")):
        try:
            value = conn.execute(f"SELECT MIN({_ts(col)}) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            continue
        if value and (best is None or value < best):
            best = value
    if not best:
        return None
    try:
        return datetime.strptime(best, _FMT) + LOCAL_UTC_OFFSET
    except ValueError:
        return None


def build(conn: sqlite3.Connection, period_key: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Всё для страницы: период, вкладки и плитки."""
    key = normalize(period_key)
    first = first_record(conn) if key == "all" else None
    period = build_period(key, now, first)
    points = buckets(period)
    tiles = [_tile(conn, m, period, points) for m in metrics()]
    for tile in tiles:
        if tile["key"] == "moderation":
            tile["note"] = _queue_note(conn)
    return {"period": period, "periods": PERIODS, "tiles": tiles, "points": len(points)}
