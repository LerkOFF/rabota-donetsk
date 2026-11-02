"""Афиши: метка «нужна афиша» и перечтение постов, у которых вакансия получилась мусорной или не создалась.

Когда вакансия лежит на картинке, а текст поста короткий (часто только подпись с телефоном), результат зависит
от распознавания. Если афиша не прочиталась, вакансия не создаётся, а пост получает метку в таблице `poster_state`:

    needs_poster   афишу не удалось прочитать; повтор при следующем обходе, если картинка изменилась
                   или распознавание улучшено (версия OCR_VERSION выросла)
    read           афиша прочитана (вакансия есть или это не вакансия)

    python -m rabota reparse-posters [--dry-run] [--source X]   перечитать такие посты улучшенным распознаванием

Кандидаты на перечтение:
1. посты с меткой needs_poster, у которых версия распознавания старее текущей или изменились картинки;
2. посты, у которых вакансия получилась мусорной (название не проходит фильтр quality), даже если её уже скрыли вручную;
3. посты с картинками и коротким текстом, по которым нет ни вакансий, ни метки (разобраны до появления метки).

Мусорная вакансия после успешного перечтения закрывается пометкой «Заменена после перечтения афиши»;
новая вакансия сохраняется обычным путём (стоп-лист, фильтр мусора, склейка дублей). Повторный запуск ничего не дублирует.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rabota.models import RawPost
from rabota.pipeline.quality import profession_problem
from rabota.vision.extract import SHORT_TEXT_CHARS, ImageScan, scan_images
from rabota.vision.images import VisionCache

log = logging.getLogger("rabota.ops.poster_reparse")

NEEDS = "needs_poster"
READ = "read"
REPLACED_NOTE = "Заменена после перечтения афиши"

SCHEMA = """
CREATE TABLE IF NOT EXISTS poster_state (
  source TEXT NOT NULL, external_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('needs_poster','read')),
  image_key TEXT NOT NULL DEFAULT '', ocr_version INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def current_version() -> int:
    from rabota.vision.ocr import OCR_VERSION

    return OCR_VERSION


def urls_key(urls: list[str]) -> str:
    """Короткий отпечаток списка картинок поста: изменился список, значит, афишу стоит перечитать."""
    return hashlib.sha1("\n".join(urls).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def set_state(conn: sqlite3.Connection, raw: RawPost, status: str, image_key: str = "", note: str = "") -> None:
    ensure_table(conn)
    conn.execute(
        "INSERT INTO poster_state(source, external_id, status, image_key, ocr_version, note, updated_at)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, external_id) DO UPDATE SET status=excluded.status,"
        " image_key=excluded.image_key, ocr_version=excluded.ocr_version, note=excluded.note, updated_at=excluded.updated_at",
        (raw.source, raw.external_id, status, image_key or urls_key(raw.image_urls), current_version(), note[:300], _now()),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, source: str, external_id: str) -> dict[str, Any] | None:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM poster_state WHERE source=? AND external_id=?", (source, external_id)).fetchone()
    return dict(row) if row else None


def record_scan(conn: sqlite3.Connection, raw: RawPost, scan: ImageScan, *, has_text_vacancies: bool) -> str | None:
    """Пометить пост по итогу чтения картинок. Возвращает поставленный статус или None, если метка не нужна."""
    if not scan.attempted:
        return None
    if scan.vacancies or has_text_vacancies:
        set_state(conn, raw, READ, scan.image_key)
        return READ
    if scan.unread and len((raw.text or "").strip()) < SHORT_TEXT_CHARS and _text_is_not_a_listing(raw):
        set_state(conn, raw, NEEDS, scan.image_key, "; ".join(scan.reasons) or "афиша не прочиталась")
        return NEEDS
    set_state(conn, raw, READ, scan.image_key)
    return READ


def _text_is_not_a_listing(raw: RawPost) -> bool:
    """Текст поста сам по себе не вакансия, услуга, набор на службу или реклама: значит, смысл мог быть на афише."""
    from rabota.pipeline.run import classify_post

    return not (raw.text or "").strip() or classify_post(raw) in ("other", "vacancy")


def needs_retry(state: dict[str, Any] | None, raw: RawPost) -> bool:
    """Нужно ли перечитывать пост с меткой: картинки изменились или распознавание стало новее."""
    if state is None or state["status"] != NEEDS:
        return False
    return state["ocr_version"] < current_version() or state["image_key"] != urls_key(raw.image_urls)


# ------------------------------------------------------------ перечтение

class _RefreshCache(VisionCache):
    """Кэш для перечтения: старые ответы и счёт баннеров игнорируются, новые ответы записываются."""

    def __init__(self, real: VisionCache, *, write: bool = True):
        super().__init__(real.dir)
        self._write = write

    def get(self, h: str, max_distance: int = 0):
        return None

    def is_banner(self, h: str, min_posts: int = 3) -> bool:
        return False

    def note_seen(self, h: str, post_key: str) -> int:
        return 0

    def put(self, h: str, data: dict[str, Any]) -> None:
        if self._write:
            super().put(h, data)


def _raw_from_row(r: sqlite3.Row) -> RawPost:
    return RawPost(
        r["source_type"], r["source"], r["external_id"], r["url"], r["text"], r["published_at"],
        json.loads(r["image_urls"] or "[]"), json.loads(r["extra"] or "{}"),
    )


def candidates(conn: sqlite3.Connection, source: str | None = None) -> list[tuple[RawPost, str]]:
    """Посты для перечтения с причиной: needs_poster, junk, unmarked."""
    ensure_table(conn)
    rows = conn.execute("SELECT * FROM raw_posts WHERE image_urls != '[]' ORDER BY id").fetchall()
    junk_posts: set[tuple[str, str]] = set()
    have_vacancy: set[tuple[str, str]] = set()
    for v in conn.execute(
        "SELECT source, external_id, profession, moderation_status, moderation_note, kind FROM vacancies"
    ):
        key = (v["source"], v["external_id"])
        have_vacancy.add(key)
        replaced = v["moderation_status"] == "rejected" and (v["moderation_note"] or "").startswith(REPLACED_NOTE)
        if v["kind"] == "vacancy" and not replaced and profession_problem(v["profession"]):
            junk_posts.add(key)
    states = {(r["source"], r["external_id"]): dict(r) for r in conn.execute("SELECT * FROM poster_state")}
    out: list[tuple[RawPost, str]] = []
    for r in rows:
        if source and source not in (r["source"], r["source_type"]):
            continue
        raw = _raw_from_row(r)
        key = (raw.source, raw.external_id)
        if key in junk_posts:
            out.append((raw, "junk"))
        elif key in states:
            if needs_retry(states[key], raw):
                out.append((raw, NEEDS))
        elif key not in have_vacancy and len((raw.text or "").strip()) < SHORT_TEXT_CHARS:
            out.append((raw, "unmarked"))
    return out


def reparse(
    conn: sqlite3.Connection,
    provider: Any,
    *,
    source: str | None = None,
    dry_run: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    cache: VisionCache | None = None,
) -> dict[str, Any]:
    """Перечитать посты-кандидаты. dry_run: распознавание идёт, база и кэш не меняются."""
    from rabota.admin.stoplist import filter_vacancies
    from rabota.db import save_vacancy
    from rabota.pipeline.dedupe import dedupe

    real_cache = cache or VisionCache()
    stats: dict[str, Any] = {"candidates": 0, "reparsed": 0, "created": 0, "replaced": 0, "still_unread": 0, "items": []}
    for raw, reason in candidates(conn, source):
        stats["candidates"] += 1
        item: dict[str, Any] = {"post": f"{raw.source}/{raw.external_id}", "reason": reason, "professions": []}
        stats["items"].append(item)
        try:
            scan = scan_images(raw, provider, cache=_RefreshCache(real_cache, write=not dry_run), fetch=fetch)
        except Exception as exc:  # noqa: BLE001 - один пост не останавливает перечтение
            log.warning("перечтение %s: %s", raw.url, exc)
            item["error"] = f"{type(exc).__name__}: {exc}"
            continue
        stats["reparsed"] += 1
        have = {
            r["profession"]
            for r in conn.execute("SELECT profession FROM vacancies WHERE source=? AND external_id=?", (raw.source, raw.external_id))
            if not profession_problem(r["profession"])
        }
        new = [v for v in scan.vacancies if v.profession not in have and not profession_problem(v.profession)]
        item["professions"] = [v.profession for v in new]
        item["unread"] = scan.unread
        if scan.unread and not new:
            stats["still_unread"] += 1
        if dry_run:
            continue
        kept, _ = filter_vacancies(conn, new)
        for vac in kept:
            save_vacancy(conn, vac, check_quality=True)
            stats["created"] += 1
        if kept:
            stats["replaced"] += _close_junk(conn, raw)
            dedupe(conn)
        record_scan(conn, raw, scan, has_text_vacancies=bool(have) or bool(kept))
    return stats


def _close_junk(conn: sqlite3.Connection, raw: RawPost) -> int:
    """Закрыть мусорные вакансии этого поста: перечитанная афиша их заменила."""
    n = 0
    for r in conn.execute(
        "SELECT id, profession, moderation_status, moderation_note FROM vacancies WHERE source=? AND external_id=? AND kind='vacancy'",
        (raw.source, raw.external_id),
    ).fetchall():
        replaced = r["moderation_status"] == "rejected" and (r["moderation_note"] or "").startswith(REPLACED_NOTE)
        if replaced or not profession_problem(r["profession"]):
            continue
        conn.execute(
            "UPDATE vacancies SET status='hidden', suspicious=1, moderation_status='rejected', moderation_note=? WHERE id=?",
            (f"{REPLACED_NOTE}.", r["id"]),
        )
        n += 1
    conn.commit()
    return n
