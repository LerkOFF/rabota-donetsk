"""Архив просроченных вакансий.

Жизнь вакансии: active -> (срок жизни) expired -> (ещё `expired_keep_days`) archived -> (срок архива) удалена.

- `expired` и закрытая работодателем вакансия через `expired_keep_days` после последней публикации переходят в `archived`:
  строка остаётся, в выдачу, поиск, подписки, рассылку и карту сайта не попадает (все они требуют status = 'active');
- у архивной вакансии через `archive_raw_days` очищается сырой текст источника (текст, картинки, extra), поля карточки
  (профессия, период, оплата, город, контакты) остаются;
- через `archive_keep_days` (настройка «Хранить архив», по умолчанию 365) архивная вакансия удаляется вместе с дублями;
- повторно собранная вакансия оживает из архива без дубля (`revive_seen`, склейка дублей в rabota.pipeline.dedupe);
- страница работодателя показывает архив вкладкой (`company_archive`), страница закрыта от индекса.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from rabota.lifecycle import is_stale, to_utc_iso

# архивная вакансия, которую можно показывать на странице работодателя
ARCHIVE_SQL = (
    "status = 'archived' AND duplicate_of IS NULL AND kind = 'vacancy' AND suspicious = 0 "
    "AND moderation_status = 'approved'"
)
ARCHIVE_PER_PAGE = 20


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _cutoff(days: int, now: datetime | None) -> str:
    return (_now(now) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _stamp(now: datetime | None) -> str:
    return _now(now).replace(microsecond=0).isoformat()


def keep_days(conn: sqlite3.Connection | None) -> int:
    from rabota.admin import settings

    return int(settings.effective(conn, "archive_keep_days"))


def raw_days(conn: sqlite3.Connection | None) -> int:
    from rabota.admin import settings

    return int(settings.effective(conn, "archive_raw_days"))


def archive_expired(
    conn: sqlite3.Connection, ttl_days: int | None = None, expired_keep: int | None = None, now: datetime | None = None,
) -> int:
    """Перенести в архив истёкшие и закрытые вакансии, которые лежат скрытыми дольше `expired_keep_days`. Коммитит сам."""
    from rabota.admin import settings

    ttl = settings.ttl_days(conn) if ttl_days is None else ttl_days
    keep = settings.keep_days(conn) if expired_keep is None else expired_keep
    cur = conn.execute(
        "UPDATE vacancies SET status = 'archived', archived_at = ? WHERE status IN ('expired', 'closed') "
        "AND COALESCE(last_seen_at, published_at) IS NOT NULL AND COALESCE(last_seen_at, published_at) < ?",
        (_stamp(now), _cutoff(ttl + keep, now)),
    )
    conn.commit()
    return cur.rowcount


def clear_raw(conn: sqlite3.Connection, days: int | None = None, now: datetime | None = None) -> int:
    """Очистить сырой текст источника у давно архивных вакансий. Сама запись сырого поста остаётся: по ней
    сборщик понимает, что пост уже видели. Пост, на который опирается живая вакансия, не трогаем."""
    limit = raw_days(conn) if days is None else days
    cur = conn.execute(
        "UPDATE raw_posts SET text = '', image_urls = '[]', extra = '{}' "
        "WHERE (text != '' OR image_urls != '[]' OR extra != '{}') "
        "AND EXISTS (SELECT 1 FROM vacancies v WHERE v.source = raw_posts.source AND v.external_id = raw_posts.external_id "
        "AND v.status = 'archived' AND COALESCE(v.archived_at, v.last_seen_at) < ?) "
        "AND NOT EXISTS (SELECT 1 FROM vacancies w WHERE w.source = raw_posts.source AND w.external_id = raw_posts.external_id "
        "AND w.status != 'archived')",
        (_cutoff(limit, now),),
    )
    conn.commit()
    return cur.rowcount


def purge_archive(conn: sqlite3.Connection, days: int | None = None, now: datetime | None = None) -> int:
    """Удалить архивные вакансии старше срока хранения вместе с дублями, жалобами, откликами и сырыми постами.
    Возвращает число удалённых вакансий (главных и дублей). Коммитит сам."""
    limit = keep_days(conn) if days is None else days
    ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM vacancies WHERE status = 'archived' AND duplicate_of IS NULL "
            "AND COALESCE(archived_at, last_seen_at, published_at) < ?",
            (_cutoff(limit, now),),
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
    conn.execute(
        f"UPDATE conversations SET vacancy_id = NULL, application_id = NULL WHERE vacancy_id IN ({m2}) "
        f"OR application_id IN (SELECT id FROM applications WHERE vacancy_id IN ({m2}))", all_ids + all_ids)
    conn.execute(f"DELETE FROM applications WHERE vacancy_id IN ({m2})", all_ids)
    conn.execute(f"DELETE FROM favorites WHERE vacancy_id IN ({m2})", all_ids)
    conn.execute(f"DELETE FROM complaints WHERE vacancy_id IN ({m2})", all_ids)
    conn.execute(f"DELETE FROM vacancies WHERE id IN ({m2})", all_ids)
    for r in rows:
        left = conn.execute(
            "SELECT 1 FROM vacancies WHERE source = ? AND external_id = ? LIMIT 1", (r["source"], r["external_id"])).fetchone()
        if left is None:
            conn.execute("DELETE FROM raw_posts WHERE source = ? AND external_id = ?", (r["source"], r["external_id"]))
    conn.commit()
    return len(all_ids)


def revive_seen(
    conn: sqlite3.Connection, source: str, external_id: str, seen_at: str | None, now: datetime | None = None,
) -> int:
    """Вакансию собрали снова (тот же источник и номер, дата новее): возвращаем из архива или из скрытых по сроку
    на сайт без дубля. Возвращает число ожививших вакансий. Коммитит сам."""
    seen = to_utc_iso(seen_at)
    if not seen or is_stale(seen, now=_now(now)):
        return 0
    cur = conn.execute(
        "UPDATE vacancies SET status = 'active', archived_at = NULL, last_seen_at = ? "
        "WHERE source = ? AND external_id = ? AND status IN ('archived', 'expired') AND moderation_status = 'approved' "
        "AND COALESCE(last_seen_at, '') < ?",
        (seen, source, external_id, seen),
    )
    conn.commit()
    return cur.rowcount


def revive(conn: sqlite3.Connection, vacancy_id: int, now: datetime | None = None) -> bool:
    """Вернуть вакансию из архива вручную (кабинет, админка): срок жизни начинается заново."""
    stamp = _stamp(now)
    cur = conn.execute(
        "UPDATE vacancies SET status = 'active', archived_at = NULL, last_seen_at = ?, published_at = COALESCE(published_at, ?) "
        "WHERE id = ? AND status = 'archived'", (stamp, stamp, vacancy_id))
    conn.commit()
    return cur.rowcount > 0


def run_all(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    """Ежедневная уборка архива: перенос, очистка сырого текста, удаление по сроку."""
    moved = archive_expired(conn, now=now)
    cleared = clear_raw(conn, now=now)
    deleted = purge_archive(conn, now=now)
    return {"в архив": moved, "очищено постов": cleared, "удалено": deleted}


# ------------------------------------------------------------------ чтение для страницы работодателя

def company_archive(conn: sqlite3.Connection, company_id: int, page: int = 1, per_page: int = ARCHIVE_PER_PAGE) -> tuple[list[dict[str, Any]], int]:
    """Архивные вакансии компании, новые сверху: (строки страницы, всего)."""
    from rabota import config
    from rabota.web.queries import city_scope_sql

    scope = city_scope_sql(config.SERVICE_CITIES)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM vacancies WHERE company_id = ? AND {ARCHIVE_SQL} AND {scope}", (company_id,)).fetchone()[0])
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    rows = conn.execute(
        f"SELECT id, profession, city, district, pay_min, pay_max, pay_period, published_at, last_seen_at, archived_at "
        f"FROM vacancies WHERE company_id = ? AND {ARCHIVE_SQL} AND {scope} "
        "ORDER BY COALESCE(published_at, last_seen_at) DESC, id DESC LIMIT ? OFFSET ?",
        (company_id, per_page, (page - 1) * per_page),
    ).fetchall()
    return [dict(r) for r in rows], total
