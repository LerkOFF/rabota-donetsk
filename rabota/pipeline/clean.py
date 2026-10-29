"""Очистка текста поста: буквы-двойники, эмодзи, хештеги, ссылки, подписи площадок, капс, пробелы.

Маркеры списков (эмодзи, точки, дефисы в начале строки) приводятся к "- ", нумерация к "1. ":
по ним потом режем пост на несколько вакансий.
"""
from __future__ import annotations

import re

from rabota.pipeline.dictionaries import ABBREVIATIONS, PROPER_STEMS, SHORT_WORDS, norm
from rabota.pipeline.homoglyphs import fix_homoglyphs

_ZERO_WIDTH = re.compile("[\u200b‌‍⁠﻿­]")
_SPACES = re.compile("[  -   　\t]")
_DASHES = re.compile("[\u2012\u2013\u2014\u2015\u2212\u2011]")

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿⌀-⏿←-⇿"
    "■-◿‼⁉⤴⤵〰〽㊗㊙︎️⃣©®™"
    "\U000E0020-\U000E007F]"
)
# символы-маркеры в начале строки (после них идёт текст пункта)
_LEAD_MARKS = re.compile(
    "^[\\s•▪◦·●○■□▶►➤➡\\-*>~★☆"
    "\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿←-⇿‼⁉︎️‍⃣]+"
)
_NUMBERED = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(?!\d)")

_URL = re.compile(
    r"https?://\S+|\b(?:t\.me|telegram\.me|vk\.cc|vk\.com|vk\.me|max\.ru|clck\.ru|wa\.me|instagram\.com|youtu\.be|"
    r"forms\.yandex\.ru|bit\.ly|goo\.gl)/\S*|\bwww\.\S+"
)
_HASHTAG = re.compile(r"(?<![\w/])#[\wЀ-ӿ]+")

# строки-подписи площадок: удаляем целиком
_BOILERPLATE_LINES = [
    re.compile(r"^\W*(?:разместить(?:\s+вакансию)?|разместить\s+объявление|подписаться|подписывайся|подпишись)\W*(?:\|.*)?$"),
    re.compile(r"^\W*разместить\s+вакансию\W*(?:\|.*)?$"),
    re.compile(r"^\W*работа\s+в\s+м[аa][хx]\W*$"),
    re.compile(r"^\W*(?:категория\s*:\s*)?вакансия\W*$"),
    re.compile(r"^\W*pinned\s+a\s+photo\W*$|^.*\bpinned\s+a\s+photo\b.*$"),
    re.compile(r"^\W*подпиш\w*.*новых\s+вакансиях.*$"),
    re.compile(r"^\W*@\w+\s+работа\s+днр\W*$"),
    re.compile(r"^\W*(?:telegram|телеграм)\s*\|.*$"),
]
# фразы внутри строки
_BOILERPLATE_INLINE = [
    re.compile(r"разместить\s+вакансию\s*\|?\s*подписаться", re.IGNORECASE),
    re.compile(r"\|\s*подписаться", re.IGNORECASE),
    re.compile(r"работа\s+в\s+м[аa][хx]\b", re.IGNORECASE),
    re.compile(r"(?<![\w])разместить\s*$", re.IGNORECASE),
]

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]*")


def _is_caps(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def _is_proper(word: str) -> bool:
    low = norm(word)
    return any(low.startswith(stem) and len(low) <= len(stem) + 5 for stem in PROPER_STEMS)


_ONE_LETTER_WORDS = {"В", "С", "К", "У", "И", "О", "А"}


def normalize_caps(line: str) -> str:
    """Слова капсом в нормальный регистр. Аббревиатуры (ООО, ДНР, ЗП) остаются."""
    words = [m for m in _WORD.finditer(line) if sum(c.isalpha() for c in m.group()) >= 2]
    if not words:
        return line
    all_caps = all(_is_caps(m.group()) for m in words)
    changed = False
    out = []
    pos = 0
    for m in _WORD.finditer(line):
        w = m.group()
        new = w
        if all_caps and w in _ONE_LETTER_WORDS and not (m.start() and line[m.start() - 1].isdigit()):
            new = w.lower()
        elif _is_caps(w) and not any(c.isdigit() for c in w) and w.upper() not in ABBREVIATIONS and w.replace("-", "").upper() not in ABBREVIATIONS:
            letters = sum(c.isalpha() for c in w)
            if all_caps or letters >= 5 or norm(w) in SHORT_WORDS:
                new = w.lower()
                if _is_proper(new):
                    new = new[0].upper() + new[1:]
        if new != w:
            changed = True
        out.append(line[pos:m.start()])
        out.append(new)
        pos = m.end()
    out.append(line[pos:])
    result = "".join(out)
    if changed:
        result = re.sub(r"([«\"])([a-zа-яё])", lambda m: m.group(1) + m.group(2).upper(), result) if all_caps else result
        # первая буква строки (после маркера) заглавная, если мы её понизили
        m = re.search(r"[A-Za-zА-Яа-яЁё]", result)
        if m and result[m.start()].islower() and _first_word_was_caps(line):
            result = result[:m.start()] + result[m.start()].upper() + result[m.start() + 1:]
    return result


def _first_word_was_caps(line: str) -> bool:
    m = _WORD.search(line)
    return bool(m and _is_caps(m.group()))


def _strip_line(line: str) -> str:
    line = _SPACES.sub(" ", line)
    line = re.sub(r" {2,}", " ", line)
    return line.strip()


def _normalize_marker(line: str) -> str:
    """Начало строки: эмодзи и точки -> "- ", нумерация -> "1. "."""
    num = _NUMBERED.match(line)
    if num:
        rest = line[num.end():]
        rest = _LEAD_MARKS.sub("", rest) if rest[:1] and not rest[:1].isalnum() else rest
        return f"{num.group(1)}. {rest.strip()}"
    m = _LEAD_MARKS.match(line)
    if m and m.end() > 0:
        rest = line[m.end():]
        # маркер был, если там есть что-то кроме пробелов
        if line[:m.end()].strip():
            return f"- {rest.strip()}" if rest.strip() else ""
        return rest
    return line


def clean_text(text: str, *, homoglyphs: bool = True) -> str:
    """Полная очистка текста поста до вида, пригодного для разбора и показа."""
    if not text:
        return ""
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if homoglyphs:
        text = fix_homoglyphs(text)
    text = _URL.sub(" ", text)
    text = _HASHTAG.sub(" ", text)
    text = _DASHES.sub("-", text)

    lines_out: list[str] = []
    for raw_line in text.split("\n"):
        line = _SPACES.sub(" ", raw_line)
        probe = _EMOJI.sub("", line).strip().lower()
        if any(p.match(probe) for p in _BOILERPLATE_LINES):
            continue
        for p in _BOILERPLATE_INLINE:
            line = p.sub(" ", line)
        line = _normalize_marker(line.strip())
        line = _EMOJI.sub(" ", line)
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        line = re.sub(r"(\d)\.(?=[A-Za-zА-Яа-яЁё])", r"\1. ", line) if _NUMBERED.match(line) else line
        line = normalize_caps(line)
        line = _strip_line(line)
        line = re.sub(r"[!?]{2,}", lambda m: m.group()[0], line)
        line = re.sub(r"^(-?\s*)[!?.]+\s*(?=\S)", r"\1", line)
        line = re.sub(r"^-\s*$", "", line)
        lines_out.append(line)
    result = "\n".join(lines_out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
