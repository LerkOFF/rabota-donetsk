"""Склейка дублей между источниками: одна вакансия, одна карточка.

Вакансии считаются дублями, если у них одна нормализованная профессия и выполняется одно из условий:
1. совпал телефон (любое написание: +7 949 ..., 8949..., 7949... сводится к +7949...);
2. совпал ключ `dedupe_key` (та же профессия и тот же текст без телефона);
3. тексты поста после очистки похожи: доля общих шинглов (троек слов) не ниже SIMILARITY_THRESHOLD.
Город с обеих сторон известный и разный - не дубль (сеть магазинов в двух городах).
Главная - самая ранняя по published_at, при равенстве та, у которой больше заполненных полей.
Поля, которых нет у главной (телефон, оплата, район), переносятся в неё из дублей.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from typing import Any

from rabota.models import Vacancy
from rabota.pipeline.clean import clean_text
from rabota.pipeline.dictionaries import norm

# порог подобран на выгрузках data/samples: пары без общего телефона дают сходство 1.0 (копии) и 0.5 (репост
# с правками), а между 0.3 и 0.5 пар нет вовсе; берём 0.45 с небольшим запасом вниз
SIMILARITY_THRESHOLD = 0.45
SHINGLE = 3
PAY_CONFLICT_SIMILARITY = 0.5     # при разной оплате общий телефон склеивает только похожие тексты
SAME_SOURCE_SIMILARITY = 0.8      # один канал, общий телефон: повтор поста должен быть почти дословным
MIN_SHINGLES_FOR_TEXT_MATCH = 6   # совсем короткие тексты по сходству не склеиваем

_PHONE_ANY = re.compile(r"(?:\+7|\b[78])[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}|\+7[\s\-(]*\d{3}[\s\-)]*\d[\d\s\-]{6,12}")
_TOKEN = re.compile(r"[a-zа-я0-9]{2,}")


# ---------------------------------------------------------------- нормализация текста и телефонов

def canonical_phone(value: str) -> str | None:
    """+7 949 123-45-67, 8949..., 7949..., 949... -> +79491234567."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return "+7" + digits


def text_normalize(text: str) -> str:
    """Нормализованный текст для сравнения: без регистра, эмодзи, хештегов, ссылок, подписей, телефонов и пунктуации."""
    cleaned = clean_text(text)
    cleaned = _PHONE_ANY.sub(" ", cleaned)
    cleaned = norm(cleaned)
    return " ".join(_TOKEN.findall(cleaned))


def text_hash(normalized: str) -> str:
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def shingles(normalized: str, k: int = SHINGLE) -> frozenset[int]:
    tokens = normalized.split()
    if not tokens:
        return frozenset()
    if len(tokens) <= k:
        return frozenset({zlib.crc32(" ".join(tokens).encode())})
    return frozenset(zlib.crc32(" ".join(tokens[i:i + k]).encode()) for i in range(len(tokens) - k + 1))


def similarity(a: frozenset[int], b: frozenset[int]) -> float:
    """Мера сходства: доля общих шинглов (Жаккар). Если один текст целиком внутри другого, сходство не ниже 0.9."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    jac = inter / len(a | b)
    contain = inter / min(len(a), len(b))
    if contain >= 0.97 and min(len(a), len(b)) >= MIN_SHINGLES_FOR_TEXT_MATCH:
        return max(jac, 0.9)
    return jac


def make_dedupe_key(profession: str, phones: list[str] | None, text: str = "") -> str:
    """Стабильный ключ: профессия + первый телефон, а без телефона - профессия + хеш нормализованного текста."""
    canon = sorted({p for p in (canonical_phone(x) for x in (phones or [])) if p})
    base = norm(profession).strip()
    tail = canon[0] if canon else "t:" + text_hash(text_normalize(text))
    return hashlib.sha1(f"{base}|{tail}".encode()).hexdigest()[:24]


def attach_text(vac: Vacancy, post_text: str) -> Vacancy:
    """Запомнить нормализованный текст поста на вакансии (в базу он не пишется, нужен для сравнения в памяти)."""
    vac._text_norm = text_normalize(post_text)  # type: ignore[attr-defined]
    return vac


def fingerprint_text(v: Vacancy | dict[str, Any]) -> str:
    """Текст для сравнения, если исходного текста поста под рукой нет: поля вакансии."""
    get = (lambda k: getattr(v, k, None)) if isinstance(v, Vacancy) else v.get
    parts = [get("profession"), get("title_raw"), get("summary"), get("requirements"), get("employer"), get("schedule"),
             get("district"), get("experience")]
    return text_normalize(" ".join(str(p) for p in parts if p))


# ---------------------------------------------------------------- модель для сравнения

_FILL_FIELDS = (
    "work_format", "employment", "schedule", "pay_min", "pay_max", "pay_period", "pay_monthly_estimate", "city",
    "district", "experience", "age_min", "requirements", "employer", "published_at",
)


@dataclass
class _Item:
    key: Any                       # id вакансии (в базе) или индекс (в памяти)
    vac: Vacancy
    text_norm: str
    sh: frozenset[int]
    phones: set[str]
    published: str
    existing_main: Any | None      # уже проставленный duplicate_of


def _filled(v: Vacancy) -> int:
    n = sum(1 for f in _FILL_FIELDS if getattr(v, f) not in (None, "", 0))
    n += 1 if v.pay_daily else 0
    c = v.contacts or {}
    n += sum(1 for k in ("phones", "telegram", "max", "email") if c.get(k))
    return n


def _rank(item: _Item) -> tuple[str, int, Any]:
    # раньше опубликована - главная; пустая дата хуже любой даты; затем больше полей; затем меньший id
    return (item.published or "9999", -_filled(item.vac), item.key if isinstance(item.key, int) else 0)


def _phones_of(v: Vacancy) -> set[str]:
    phones = (v.contacts or {}).get("phones") or []
    return {p for p in (canonical_phone(str(x)) for x in phones) if p}


def _compatible(a: _Item, b: _Item) -> bool:
    if a.vac.profession != b.vac.profession:
        return False
    if a.vac.city and b.vac.city and a.vac.city != b.vac.city:
        return False
    # "Центр" - слишком общее место, чтобы считать его другим районом
    da, db = a.vac.district, b.vac.district
    return not (da and db and da != db and "Центр" not in (da, db))


def _same_source(a: Vacancy, b: Vacancy) -> bool:
    return (a.source_type, (a.source or "").lower()) == (b.source_type, (b.source or "").lower())


def _pay_conflict(a: Vacancy, b: Vacancy) -> bool:
    if a.pay_min is None and a.pay_max is None:
        return False
    if b.pay_min is None and b.pay_max is None:
        return False
    return (a.pay_min, a.pay_max) != (b.pay_min, b.pay_max)


def _same_vacancy(a: _Item, b: _Item, threshold: float) -> bool:
    if not _compatible(a, b):
        return False
    if a.phones and b.phones and a.phones & b.phones:
        # один телефон у разных объявлений работодателя: разная оплата и разный текст - это разные вакансии
        sim = similarity(a.sh, b.sh)
        if _same_source(a.vac, b.vac):
            # в одном канале два разных объявления с одним телефоном: склеиваем только повтор почти того же текста
            if sim >= SAME_SOURCE_SIMILARITY:
                return True
        elif not _pay_conflict(a.vac, b.vac) or sim >= PAY_CONFLICT_SIMILARITY:
            return True
    if not (a.phones or b.phones) and a.vac.dedupe_key and a.vac.dedupe_key == b.vac.dedupe_key:
        return True
    if len(a.sh) >= MIN_SHINGLES_FOR_TEXT_MATCH and len(b.sh) >= MIN_SHINGLES_FOR_TEXT_MATCH:
        if _same_source(a.vac, b.vac):
            threshold = max(threshold, SAME_SOURCE_SIMILARITY)
        return similarity(a.sh, b.sh) >= threshold
    return a.text_norm == b.text_norm and bool(a.text_norm)


def _build_item(key: Any, v: Vacancy, text: str | None, existing_main: Any | None) -> _Item:
    tn = text if text is not None else getattr(v, "_text_norm", None)
    if tn is None:
        tn = fingerprint_text(v)
    return _Item(key, v, tn, shingles(tn), _phones_of(v), v.published_at or "", existing_main)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _clusters(items: list[_Item], threshold: float) -> list[list[_Item]]:
    uf = _UnionFind(len(items))
    by_key = {it.key: i for i, it in enumerate(items)}
    for i, it in enumerate(items):
        if it.existing_main is not None and it.existing_main in by_key:
            uf.union(i, by_key[it.existing_main])
    buckets: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        buckets.setdefault(it.vac.profession, []).append(i)
    for idxs in buckets.values():
        for x, i in enumerate(idxs):
            for j in idxs[x + 1:]:
                if uf.find(i) != uf.find(j) and _same_vacancy(items[i], items[j], threshold):
                    uf.union(i, j)
    groups: dict[int, list[_Item]] = {}
    for i, it in enumerate(items):
        groups.setdefault(uf.find(i), []).append(it)
    return [g for g in groups.values() if len(g) > 1]


def merge_into(main: Vacancy, dup: Vacancy) -> bool:
    """Перенести в главную поля, которых у неё нет. Телефоны объединяются. Возвращает True, если что-то изменилось."""
    changed = False
    pay_missing = main.pay_min is None and main.pay_max is None
    if pay_missing and (dup.pay_min is not None or dup.pay_max is not None):
        main.pay_min, main.pay_max = dup.pay_min, dup.pay_max
        main.pay_period, main.pay_monthly_estimate = dup.pay_period, dup.pay_monthly_estimate
        main.pay_daily = main.pay_daily or dup.pay_daily
        changed = True
    for f in _FILL_FIELDS:
        if f.startswith("pay_"):
            continue
        if getattr(main, f) in (None, "") and getattr(dup, f) not in (None, ""):
            setattr(main, f, getattr(dup, f))
            changed = True
    if main.district == "Центр" and dup.district not in (None, "", "Центр"):
        main.district = dup.district
        changed = True
    if dup.pay_daily and not main.pay_daily:
        main.pay_daily = True
        changed = True
    merged = dict(main.contacts or {})
    phones = list(merged.get("phones") or [])
    for p in (dup.contacts or {}).get("phones") or []:
        if p not in phones and len(phones) < 5:
            phones.append(p)
    if phones and phones != merged.get("phones"):
        merged["phones"] = phones
        changed = True
    for k in ("telegram", "max", "email"):
        if not merged.get(k) and (dup.contacts or {}).get(k):
            merged[k] = dup.contacts[k]
            changed = True
    if not main.summary and dup.summary:
        main.summary = dup.summary
        changed = True
    if dup.suspicious and not main.suspicious:
        main.suspicious = True
        changed = True
    main.contacts = merged
    return changed


# ---------------------------------------------------------------- пачка в памяти

def assign_duplicates(
    vacancies: list[Vacancy],
    ids: list[int] | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[Vacancy]:
    """Пометить дубли в списке: duplicate_of = id главной, поля дублей сливаются в главную.

    ids - идентификаторы вакансий (по порядку списка); без них берутся номера с единицы.
    Уже проставленный duplicate_of не сбрасывается. Повторный вызов результат не меняет.
    """
    ids = ids or list(range(1, len(vacancies) + 1))
    items = [
        _build_item(ids[i], v, None, v.duplicate_of)
        for i, v in enumerate(vacancies)
        if v.kind == "vacancy" and v.status != "hidden"
    ]
    for group in _clusters(items, threshold):
        main = _choose_main(group)
        for it in group:
            if it is main:
                continue
            it.vac.duplicate_of = main.key
            merge_into(main.vac, it.vac)
        main.vac.duplicate_of = None
    return vacancies


def _choose_main(group: list[_Item]) -> _Item:
    keys = {it.key for it in group}
    # уже существующие главные (на них ссылаются другие) остаются главными: повторный запуск ничего не ломает
    pointed = {it.existing_main for it in group if it.existing_main in keys}
    existing = [it for it in group if it.key in pointed]
    return min(existing or group, key=_rank)


# ---------------------------------------------------------------- база

_VAC_SELECT = (
    "SELECT v.*, r.text AS raw_text FROM vacancies v "
    "LEFT JOIN raw_posts r ON r.source = v.source AND r.external_id = v.external_id "
    "WHERE v.kind = 'vacancy' AND v.status != 'hidden' ORDER BY v.id"
)


def _row_to_vacancy(row: sqlite3.Row) -> Vacancy:
    d = dict(row)
    names = Vacancy.__dataclass_fields__.keys()
    data = {k: d[k] for k in names if k in d}
    data["contacts"] = json.loads(d.get("contacts") or "{}")
    data["pay_daily"] = bool(d.get("pay_daily"))
    data["suspicious"] = bool(d.get("suspicious"))
    return Vacancy(**data)


def dedupe(conn: sqlite3.Connection, threshold: float = SIMILARITY_THRESHOLD) -> int:
    """Склеить дубли в таблице vacancies. Возвращает число вновь помеченных дублей.

    Повторный запуск идемпотентен: существующие главные остаются главными, проставленные ссылки сохраняются.
    """
    rows = conn.execute(_VAC_SELECT).fetchall()
    items: list[_Item] = []
    for r in rows:
        v = _row_to_vacancy(r)
        raw_text = r["raw_text"]
        tn = text_normalize(raw_text) if raw_text else None
        items.append(_build_item(int(r["id"]), v, tn, v.duplicate_of))
    before = {it.key: it.vac.duplicate_of for it in items}
    marked = 0
    for group in _clusters(items, threshold):
        main = _choose_main(group)
        for it in group:
            if it is main:
                if it.vac.duplicate_of is not None:
                    it.vac.duplicate_of = None
                continue
            it.vac.duplicate_of = main.key
            if merge_into(main.vac, it.vac):
                _save_merged(conn, main)
        for it in group:
            if it is not main and before[it.key] != it.vac.duplicate_of:
                conn.execute("UPDATE vacancies SET duplicate_of = ? WHERE id = ?", (it.vac.duplicate_of, it.key))
                if before[it.key] is None:
                    marked += 1
        if before[main.key] is not None:
            conn.execute("UPDATE vacancies SET duplicate_of = NULL WHERE id = ?", (main.key,))
    conn.commit()
    return marked


def _save_merged(conn: sqlite3.Connection, main: _Item) -> None:
    v = main.vac
    conn.execute(
        "UPDATE vacancies SET work_format=?, employment=?, schedule=?, pay_min=?, pay_max=?, pay_period=?, pay_daily=?,"
        " pay_monthly_estimate=?, city=?, district=?, experience=?, age_min=?, requirements=?, employer=?, contacts=?,"
        " summary=?, suspicious=?, published_at=? WHERE id=?",
        (v.work_format, v.employment, v.schedule, v.pay_min, v.pay_max, v.pay_period, int(v.pay_daily),
         v.pay_monthly_estimate, v.city, v.district, v.experience, v.age_min, v.requirements, v.employer,
         json.dumps(v.contacts, ensure_ascii=False), v.summary, int(v.suspicious), v.published_at, main.key),
    )


def also_published_in(conn: sqlite3.Connection, vacancy_id: int) -> list[dict[str, str]]:
    """Где ещё опубликована вакансия: источники дублей (source_type, source, source_url), без повторов."""
    cur = conn.execute(
        "SELECT source_type, source, source_url FROM vacancies WHERE duplicate_of = ? ORDER BY published_at, id",
        (vacancy_id,),
    )
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for r in cur:
        key = (r["source_type"], r["source"], r["source_url"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"source_type": key[0], "source": key[1], "source_url": key[2]})
    return out
