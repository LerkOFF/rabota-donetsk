"""Замена латинских и греческих букв-двойников на кириллицу внутри русских слов.

Подмена нужна, чтобы обойти фильтры и поиск: "Тpeбуeтся", "ЛΒИ", "ДΗР", "Hе кocмeтикa".
Слово правим, только если в нём уже есть кириллица: чистая латиница ("Telegram", "iPhone")
остаётся как есть.
"""
from __future__ import annotations

import re

LATIN_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О", "P": "Р",
    "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "k": "к", "m": "м", "o": "о", "p": "р", "x": "х", "y": "у",
}

GREEK_TO_CYR = {
    "Α": "А", "Β": "В", "Ε": "Е", "Ζ": "З", "Η": "Н", "Κ": "К", "Μ": "М", "Ο": "О", "Ρ": "Р",
    "Τ": "Т", "Χ": "Х", "Υ": "У",
    "α": "а", "ο": "о", "ρ": "р", "χ": "х", "κ": "к",
}

HOMOGLYPHS = {**LATIN_TO_CYR, **GREEK_TO_CYR}

# слова из одних латинских двойников, которые на самом деле русские аббревиатуры
LATIN_WORD_FIXES = {"OOO": "ООО", "CTO": "СТО", "TK": "ТК", "TPЦ": "ТРЦ"}

_WORD = re.compile(r"\w+")
_CYR = re.compile(r"[А-Яа-яЁё]")
_FOREIGN = re.compile(r"[A-Za-zΑ-Ωα-ω]")
_ZERO_IN_WORD = re.compile(r"(?<=[А-Яа-яЁё])0(?=[А-Яа-яЁё])")


def _fix_token(token: str) -> str:
    has_cyr = bool(_CYR.search(token))
    has_foreign = bool(_FOREIGN.search(token))
    if not has_foreign:
        if has_cyr and "0" in token:
            return _ZERO_IN_WORD.sub("о", token)
        return token
    if has_cyr:
        out = "".join(HOMOGLYPHS.get(ch, ch) for ch in token)
        return _ZERO_IN_WORD.sub("о", out)
    if token in LATIN_WORD_FIXES:
        return LATIN_WORD_FIXES[token]
    # слово целиком из греческих букв-двойников: "ΗΡ" и подобное
    if all(ch in GREEK_TO_CYR for ch in token):
        return "".join(GREEK_TO_CYR[ch] for ch in token)
    return token


def fix_homoglyphs(text: str) -> str:
    """Заменить буквы-двойники на кириллицу в словах, где уже есть кириллица."""
    return _WORD.sub(lambda m: _fix_token(m.group(0)), text)


def has_homoglyphs(text: str) -> bool:
    """Есть ли в тексте подмена букв (для метрик и подозрительных постов)."""
    return fix_homoglyphs(text) != text
