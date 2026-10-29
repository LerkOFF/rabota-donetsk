"""Стоп-лист: телефоны, ники, названия работодателей и ключевые слова, из-за которых вакансия не публикуется.

Конвейер (разбор постов и приём в реальном времени) пропускает вакансии через `drop_stoplisted`.
Для запросов по персональным данным запись хранится в виде хеша: заблокировать контакт нужно, а сам номер хранить нет.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rabota.admin import audit
from rabota.admin.common import actor_id, clean_line, iso, now_utc

KINDS = {"phone": "Телефон", "nick": "Ник", "employer": "Работодатель", "keyword": "Ключевое слово"}
HASH_PREFIX = "sha256:"
TEXT_FIELDS = ("profession", "title_raw", "summary", "requirements", "employer")
_NICK_RE = re.compile(r"^[a-z0-9_]{3,64}$")
_QUOTES = str.maketrans("", "", "«»\"'`„“”")


def fold(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def normalize(kind: str, value: str) -> str | None:
    """Приводит значение к виду для сравнения. None: значение не годится для этого вида."""
    raw = str(value or "").strip()
    if kind == "phone":
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits[0] in "78":
            digits = digits[1:]
        return digits if len(digits) == 10 else None
    if kind == "nick":
        nick = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", raw.lower()).lstrip("@").strip("/")
        return nick if _NICK_RE.match(nick) else None
    if kind == "employer":
        name = fold(raw.translate(_QUOTES))
        return name if len(name) >= 3 else None
    if kind == "keyword":
        word = fold(raw)
        return word if len(word) >= 3 else None
    return None


def _hash(kind: str, normalized: str) -> str:
    return HASH_PREFIX + hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


def _mask(kind: str, raw: str, normalized: str) -> str:
    if kind == "phone":
        return f"+7{normalized[:3]}***{normalized[-2:]}"
    return normalized[:2] + "***"


def add(
    conn: sqlite3.Connection, kind: str, value: str, *, actor: dict[str, Any] | None = None, note: str = "",
    source: str = "manual", hashed: bool = False, now: datetime | None = None,
) -> tuple[int, bool]:
    """Добавить запись. Возвращает (id, создана ли новая). ValueError, если значение не годится."""
    if kind not in KINDS:
        raise ValueError("Неизвестный вид записи стоп-листа.")
    normalized = normalize(kind, value)
    if normalized is None:
        hint = {"phone": "Телефон: 10 или 11 цифр.", "nick": "Ник: латиница, цифры, подчёркивание, от 3 знаков.",
                "employer": "Название работодателя: от 3 знаков.", "keyword": "Ключевое слово: от 3 знаков."}[kind]
        raise ValueError(hint)
    if hashed and kind in ("phone", "nick"):
        stored, shown = _hash(kind, normalized), _mask(kind, value, normalized)
    else:
        stored, shown = normalized, clean_line(value, 120)
    row = conn.execute("SELECT id FROM stoplist WHERE kind=? AND normalized=?", (kind, stored)).fetchone()
    if row:
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO stoplist(kind, value, normalized, note, source, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
        (kind, shown, stored, clean_line(note, 300), source, iso(now or now_utc()), actor_id(actor)),
    )
    conn.commit()
    entry_id = int(cur.lastrowid or 0)
    audit.log(conn, actor, "stoplist.add", "stoplist", entry_id, {"kind": kind, "value": shown, "source": source}, now=now)
    return entry_id, True


def remove(conn: sqlite3.Connection, entry_id: int, actor: dict[str, Any] | None = None, now: datetime | None = None) -> bool:
    row = conn.execute("SELECT kind, value FROM stoplist WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM stoplist WHERE id=?", (entry_id,))
    conn.commit()
    audit.log(conn, actor, "stoplist.remove", "stoplist", entry_id, {"kind": row["kind"], "value": row["value"]}, now=now)
    return True


def listing(conn: sqlite3.Connection, kind: str = "") -> list[dict[str, Any]]:
    where, args = ("WHERE kind=?", [kind]) if kind in KINDS else ("", [])
    rows = conn.execute(f"SELECT * FROM stoplist {where} ORDER BY id DESC", args).fetchall()
    return [dict(r) | {"kind_label": KINDS[r["kind"]]} for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM stoplist").fetchone()[0])


@dataclass(frozen=True)
class Match:
    kind: str
    entry_id: int
    value: str


def _field(v: Any, name: str) -> Any:
    return v.get(name) if isinstance(v, Mapping) else getattr(v, name, None)


def contacts_of(v: Any) -> dict[str, Any]:
    c = _field(v, "contacts")
    if isinstance(c, str):
        try:
            c = json.loads(c or "{}")
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def phones_of(v: Any) -> set[str]:
    out = set()
    for p in contacts_of(v).get("phones") or []:
        n = normalize("phone", str(p))
        if n:
            out.add(n)
    return out


def nicks_of(v: Any) -> set[str]:
    c = contacts_of(v)
    out = set()
    for key in ("telegram", "max"):
        n = normalize("nick", str(c.get(key) or ""))
        if n:
            out.add(n)
    return out


def text_of(v: Any) -> str:
    return fold(" ".join(str(_field(v, f) or "") for f in TEXT_FIELDS))


class Stoplist:
    """Снимок стоп-листа в памяти: одна загрузка на пачку вакансий."""

    def __init__(self, entries: Iterable[Mapping[str, Any]] = ()):
        self.phones: dict[str, Mapping[str, Any]] = {}
        self.nicks: dict[str, Mapping[str, Any]] = {}
        self.employers: dict[str, Mapping[str, Any]] = {}
        self.keywords: list[Mapping[str, Any]] = []
        for e in entries:
            kind = e["kind"]
            if kind == "phone":
                self.phones[e["normalized"]] = e
            elif kind == "nick":
                self.nicks[e["normalized"]] = e
            elif kind == "employer":
                self.employers[e["normalized"]] = e
            elif kind == "keyword":
                self.keywords.append(e)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Stoplist:
        return cls(conn.execute("SELECT id, kind, value, normalized FROM stoplist").fetchall())

    def __bool__(self) -> bool:
        return bool(self.phones or self.nicks or self.employers or self.keywords)

    def _lookup(self, table: dict[str, Mapping[str, Any]], kind: str, normalized: str) -> Mapping[str, Any] | None:
        return table.get(normalized) or table.get(_hash(kind, normalized))

    def match(self, v: Any) -> Match | None:
        if not self:
            return None
        for p in sorted(phones_of(v)):
            e = self._lookup(self.phones, "phone", p)
            if e:
                return Match("phone", int(e["id"]), str(e["value"]))
        for n in sorted(nicks_of(v)):
            e = self._lookup(self.nicks, "nick", n)
            if e:
                return Match("nick", int(e["id"]), str(e["value"]))
        employer = normalize("employer", str(_field(v, "employer") or ""))
        if employer and employer in self.employers:
            e = self.employers[employer]
            return Match("employer", int(e["id"]), str(e["value"]))
        if self.keywords:
            text = text_of(v)
            for e in self.keywords:
                if e["normalized"] in text:
                    return Match("keyword", int(e["id"]), str(e["value"]))
        return None


def register_hits(conn: sqlite3.Connection, hits: dict[int, int], now: datetime | None = None) -> None:
    stamp = iso(now or now_utc())
    for entry_id, n in hits.items():
        conn.execute("UPDATE stoplist SET hits = hits + ?, last_hit_at = ? WHERE id = ?", (n, stamp, entry_id))
    if hits:
        conn.commit()


def drop_stoplisted(conn: sqlite3.Connection, vacancies: list[Any], now: datetime | None = None) -> list[Any]:
    """Оставляет вакансии, которых нет в стоп-листе. Срабатывания записываются в счётчики записей."""
    return filter_vacancies(conn, vacancies, now)[0]


def filter_vacancies(conn: sqlite3.Connection, vacancies: list[Any], now: datetime | None = None) -> tuple[list[Any], int]:
    """(оставшиеся вакансии, сколько отброшено)."""
    sl = Stoplist.load(conn)
    if not sl or not vacancies:
        return vacancies, 0
    kept: list[Any] = []
    hits: dict[int, int] = {}
    for v in vacancies:
        m = sl.match(v)
        if m is None:
            kept.append(v)
        else:
            hits[m.entry_id] = hits.get(m.entry_id, 0) + 1
    register_hits(conn, hits, now)
    return kept, len(vacancies) - len(kept)


def apply_to_existing(
    conn: sqlite3.Connection, actor: dict[str, Any] | None = None, entry_id: int | None = None,
    now: datetime | None = None,
) -> int:
    """Скрыть уже опубликованные вакансии, подходящие под стоп-лист (или под одну запись). Возвращает число скрытых."""
    entries = conn.execute(
        "SELECT id, kind, value, normalized FROM stoplist" + (" WHERE id=?" if entry_id else ""),
        (entry_id,) if entry_id else (),
    ).fetchall()
    sl = Stoplist(entries)
    if not sl:
        return 0
    hidden = 0
    rows = conn.execute("SELECT * FROM vacancies WHERE status='active'").fetchall()
    for r in rows:
        m = sl.match(dict(r))
        if m is None:
            continue
        note = f"Скрыто по стоп-листу: {KINDS[m.kind].lower()} {m.value}"
        conn.execute("UPDATE vacancies SET status='hidden', moderation_note=? WHERE id=?", (note[:300], r["id"]))
        hidden += 1
        audit.log(conn, actor, "vacancy.stoplist_hide", "vacancy", r["id"], {"entry": m.entry_id}, now=now)
    conn.commit()
    return hidden
