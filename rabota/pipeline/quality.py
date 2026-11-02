"""Защитный фильтр от мусора: вакансия не публикуется, если должность не похожа на должность.

Фильтр не зависит от того, откуда пришёл мусор (шум распознавания картинки, обрывок текста, сбой разбора):
проверяется само название должности.

    profession_problem("„оо")        -> "..."  (причина, по которой это не должность)
    profession_problem("грузчик")    -> None   (нормальная должность)
    vacancy_problem(vacancy)         -> причина или None

Мусорная вакансия сохраняется, но получает `suspicious=1`, `moderation_status='pending'`, `status='hidden'`
и причину в `moderation_note` (начинается с NOTE_PREFIX). Её видно в админке, в выдачу и sitemap она не попадает.
Слова проверяются по словарю профессий, по словарю слов вакансий (vocab_ru.txt) и по эвристике «похоже на слово».
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from rabota.pipeline.dictionaries import PROFESSION_SOURCE, find_professions, norm

NOTE_PREFIX = "Похоже на мусор"

MIN_LENGTH = 4
MAX_SERVICE_SHARE = 0.30
VOCAB_FILE = Path(__file__).with_name("vocab_ru.txt")

LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
THREE_LETTERS_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{3}")
REPEAT_RE = re.compile(r"(.)\1{3,}")
STRAY_CHARS_RE = re.compile("[\u201e\u201a|~_=<>{}\\[\\]\\\\^`\u2022\u00b7\u2026]")  # „ ‚ | ~ _ = < > { } [ ] \ ^ ` и точки-маркеры: так выглядит шум распознавания
CONSONANT_RUN_RE = re.compile(r"[бвгджзйклмнпрстфхцчшщbcdfghjklmnpqrstvwxz]{5,}", re.IGNORECASE)

VOWELS = set("аеёиоуыэюяaeiouy")
STOPWORDS = {
    "и", "в", "на", "по", "для", "с", "со", "из", "к", "от", "или", "а", "но", "у", "о", "об", "за", "при",
    "не", "же", "ли", "бы", "то", "до", "над", "под", "без", "про", "the", "and", "or", "of", "for", "in", "on",
}


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    """Слова вакансий: файл vocab_ru.txt плюс слова из словаря профессий."""
    words: set[str] = set()
    try:
        words.update(w.strip() for w in VOCAB_FILE.read_text(encoding="utf-8").splitlines() if w.strip())
    except OSError:
        pass
    for line in PROFESSION_SOURCE.splitlines():
        for w in WORD_RE.findall(norm(line.split("=", 1)[0] + " " + line.split("=", 1)[-1])):
            if len(w) >= 3:
                words.add(w)
    return frozenset(words)


def stem(word: str) -> str:
    """Грубая основа для сравнения форм слова: "автомойщиков" и "автомойщик" дают одно."""
    return word[: max(4, min(len(word) - 3, 7))]


@lru_cache(maxsize=1)
def _stems() -> frozenset[str]:
    return frozenset(stem(w) for w in vocabulary() if len(w) >= 4)


def is_known_word(word: str) -> bool:
    """Слово есть в словаре вакансий (в любой форме)."""
    w = norm(word)
    if w in vocabulary():
        return True
    return len(w) >= 5 and stem(w) in _stems()


def looks_like_word(word: str) -> bool:
    """Эвристика «похоже на слово»: достаточно букв, гласные и согласные в разумной доле, нет повторов и цепочек."""
    w = norm(word)
    if len(w) < 3 or not w.isalpha():
        return False
    has_cyr = bool(re.search(r"[а-я]", w))
    has_lat = bool(re.search(r"[a-z]", w))
    if has_cyr and has_lat:
        return False
    if REPEAT_RE.search(w) or CONSONANT_RUN_RE.search(w):
        return False
    vowels = sum(1 for ch in w if ch in VOWELS)
    share = vowels / len(w)
    if len(w) >= 5:
        return 0.2 <= share <= 0.7
    return vowels >= 1 and share <= 0.8


def _service_share(text: str) -> float:
    """Доля символов, которые не буквы, не цифры и не пробелы. Дефис, косая черта и плюс допустимы в названии."""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 1.0
    service = [ch for ch in chars if not (ch.isalnum() or ch in "-/+#")]
    return len(service) / len(chars)


def profession_problem(profession: str | None) -> str | None:
    """Причина, по которой строка не годится как должность. None, если должность осмысленная."""
    text = re.sub(r"\s+", " ", profession or "").strip()
    if not text:
        return "пустая должность"
    if _service_share(text) > MAX_SERVICE_SHARE:
        return "в названии почти одни знаки препинания"
    if STRAY_CHARS_RE.search(text):
        return "в названии посторонние символы (шум распознавания)"
    words = WORD_RE.findall(text)
    if words and all(norm(w) in STOPWORDS for w in words):
        return "название состоит из союзов и предлогов"
    if not THREE_LETTERS_RE.search(text):
        return "в названии нет трёх букв подряд"
    if REPEAT_RE.search(norm(text)):
        return "в названии повторяются одинаковые символы"
    known = bool(find_professions(text)) or any(is_known_word(w) for w in words if len(w) >= 3)
    if len(text) < MIN_LENGTH and not find_professions(text):
        return "название короче четырёх символов"
    if known:
        return None
    if any(looks_like_word(w) for w in words):
        return None
    return "в названии нет слова, похожего на слово"


def vacancy_problem(vacancy: Any) -> str | None:
    """Причина мусора для Vacancy или строки базы. None, если вакансию можно публиковать."""
    get = vacancy.get if isinstance(vacancy, dict) else lambda k, d=None: getattr(vacancy, k, d)
    if (get("kind", "vacancy") or "vacancy") != "vacancy":
        return None
    return profession_problem(get("profession", ""))


def note_for(reason: str, profession: str | None) -> str:
    shown = re.sub(r"\s+", " ", profession or "").strip()[:60]
    return f"{NOTE_PREFIX}: {reason}. Должность: «{shown}»."


def is_quality_note(note: str | None) -> bool:
    return bool(note) and str(note).startswith(NOTE_PREFIX)
