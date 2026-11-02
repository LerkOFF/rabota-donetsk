"""Оценка прочитанного текста: чем больше настоящих слов и меньше шума, тем выше балл.

Нужна, чтобы выбирать лучший из нескольких прогонов Tesseract не по длине (шум длинный), а по смыслу:
доля слов из словаря вакансий и профессий, доля кириллицы, отсутствие мусорных символов, телефон.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rabota.pipeline.dictionaries import GENERIC_LEMMAS, find_professions, norm
from rabota.pipeline.quality import STOPWORDS, is_known_word, looks_like_word

PHONE_RE = re.compile(r"(?<!\d)(?:\+?[78][\s\-()]*)?\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EDGE = " \t\r\n.,:;!?()[]{}<>«»\"'`„“”‘’|/\\*#=~_+-\u2014\u2013\u2022\u00b7"
_ALLOWED_PUNCT = set(".,:;!?()+-/%₽№\"'«»&@#*=\u2013\u2014\u2022\u00b7")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")

PHONE_BONUS = 20.0
PROFESSION_BONUS = 30.0
UNKNOWN_WORD_WEIGHT = 0.3
GARBAGE_WORD_WEIGHT = -0.6
JUNK_CHAR_WEIGHT = -1.0
JUNK_TOKEN_WEIGHT = -1.0


@dataclass(frozen=True)
class TextQuality:
    score: float          # итоговый балл для сравнения прогонов
    known_letters: int    # буквы в словах из словаря
    total_letters: int
    junk: int             # мусорные символы и обрывки
    has_profession: bool
    phones: int

    @property
    def known_share(self) -> float:
        return self.known_letters / self.total_letters if self.total_letters else 0.0


def assess(text: str) -> TextQuality:
    """Балл текста: слова из словаря плюс, непохожие на слова минус, мусорные символы минус, телефон и должность плюс."""
    known = total = junk = 0
    score = 0.0
    for raw in re.split(r"\s+", text or ""):
        token = raw.strip(_EDGE)
        letters = _LETTER.findall(token)
        junk_chars = sum(1 for ch in raw if not (ch.isalnum() or ch.isspace() or ch in _ALLOWED_PUNCT))
        junk += junk_chars
        score += JUNK_CHAR_WEIGHT * junk_chars
        if not letters:
            if raw and not any(ch.isdigit() for ch in raw) and len(raw) <= 2:
                junk += 1
                score += JUNK_TOKEN_WEIGHT
            continue
        word = "".join(letters)
        total += len(word)
        # цифры или знаки внутри слова: "АВТОМ0Й", "Ko4a"
        if len(token) != len(word) and not (is_known_word(word) and len(word) >= 5):
            score += GARBAGE_WORD_WEIGHT * len(word) * 0.5
            continue
        if len(word) <= 2:
            if norm(word) in STOPWORDS:
                known += len(word)
            else:
                junk += 1
                score += JUNK_TOKEN_WEIGHT * 0.5
            continue
        if is_known_word(word):
            known += len(word)
            score += len(word)
        elif looks_like_word(word):
            score += UNKNOWN_WORD_WEIGHT * len(word)
        else:
            score += GARBAGE_WORD_WEIGHT * len(word)
    phones = len({re.sub(r"\D", "", m.group(0))[-10:] for m in PHONE_RE.finditer(text or "")})
    score += PHONE_BONUS * min(phones, 2)
    hits = [h for h in find_professions(text or "") if h.lemma not in GENERIC_LEMMAS]
    if hits:
        score += PROFESSION_BONUS
    return TextQuality(score, known, total, junk, bool(hits), phones)


def readable(text: str, *, min_known_share: float = 0.35, min_known_letters: int = 25) -> bool:
    """Текст осмысленный: достаточно слов из словаря. Шум распознавания этого порога не проходит."""
    q = assess(text)
    return q.known_letters >= min_known_letters and q.known_share >= min_known_share
