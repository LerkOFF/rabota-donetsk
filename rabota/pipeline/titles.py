"""Выбор должности из текста вакансии: заголовок важнее описания."""
from __future__ import annotations

import re
from dataclasses import dataclass

from rabota.pipeline.dictionaries import (
    CITY_PATTERNS,
    GENERIC_LEMMAS,
    HIRING_RE,
    VACANCY_STRUCTURE_RE,
    ProfessionHit,
    find_professions,
    norm,
    profession_category,
)

_SECTION_CUT = re.compile(r"(?im)^\W*(?:обязанности|требования|условия|мы\s+предлагаем|что\s+предлагаем)\b")

# служебные строки карточки ("Адрес: улица Проходчиков", "Работодатель: Пекарня"): там не должность
_META_LINE = re.compile(r"^\W*(?:работодатель|адрес|город|район|зарплата|опыт\s+работы|занятость|график|часов\s+в\s+смене)\s*:", re.IGNORECASE)

_GENERIC_STOP = {
    "сотрудники", "сотрудник", "работники", "работник", "люди", "человек", "специалисты", "срочно", "помощь", "персонал",
    "кандидатов", "кандидата", "в", "на", "для", "команду", "новых", "новый", "ответственного", "ответственных",
}
_TRIGGER = re.compile(
    r"(?:требу(?:ется|ются)|нужен|нужна|нужны|ищем|ищет|приглашаем|приглашает|в\s+поисках?|вакансия\s*:?)\s+"
    r"((?:[а-яё][а-яё\-]+)(?:\s+[а-яё][а-яё\-]+){0,4})",
    re.IGNORECASE,
)
_CUT_WORDS = {"в", "на", "для", "со", "с", "к", "от", "из", "и", "или", "у", "при", "за"}


_GEO_PAREN = re.compile(r"\([^)]*(?:\bг\.|\bг\s|город|республик|област|\bкрай|днр|лнр|\bрф\b)[^)]*\)", re.IGNORECASE)
_GRADE = re.compile(r"\b\d+(?:-?(?:го|й|ый))?\s*[-]?\s*разряд\w*", re.IGNORECASE)
_TAIL_CITY = re.compile(r"\s+(?:" + "|".join(CITY_PATTERNS.values()) + r")\s*$", re.IGNORECASE)
_TAIL_CAPITAL_AFTER_COMMA = re.compile(r",\s*(?:г\.?\s*)?[А-ЯЁ][а-яё]+(?:[ -][А-ЯЁ][а-яё]+)?\s*$")
_TAIL_IN_CITY = re.compile(r"\s+в\s+[А-ЯЁ][а-яё]+(?:[ -][А-ЯЁ][а-яё]+)?\s*$")
_TAIL_LATIN_ABBR = re.compile(r"\s*/\s*[A-Za-z]{2,6}\s*$")
_TAIL_GOROD = re.compile(r",?\s*г\.\s*[А-ЯЁа-яё\- ]+$")


def clean_title_phrase(title: str) -> str:
    """Название вакансии как должность: без города в скобках и после запятой, без разряда ("Проходчик 5 разряда (г. Воркута)" -> "проходчик")."""
    t = re.sub(r"\s+", " ", title).strip()
    t = _GEO_PAREN.sub("", t)
    t = _GRADE.sub("", t)
    for _ in range(2):
        t = _TAIL_GOROD.sub("", t)
        t = _TAIL_CAPITAL_AFTER_COMMA.sub("", t)
        t = _TAIL_IN_CITY.sub("", t)
    t = _TAIL_CITY.sub("", t)
    t = _TAIL_LATIN_ABBR.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;:/-")
    return t.lower()[:80]


@dataclass
class TitleChoice:
    lemma: str
    category: str
    generic: bool = False   # слово из словаря слишком общее ("менеджер")
    known: bool = True      # из словаря, а не вытащено шаблоном


def _scrub_meta(region: str) -> str:
    """Служебные строки карточки остаются на своих местах (чтобы не сдвигать окно поиска), но их текст не читаем."""
    return "\n".join("" if _META_LINE.match(ln) else ln for ln in region.split("\n"))


def _first_good(hits: list[ProfessionHit]) -> ProfessionHit | None:
    for h in hits:
        if h.lemma not in GENERIC_LEMMAS:
            return h
    return None


def generic_title(text: str) -> str | None:
    """Должность, которой нет в словаре: берём слова после "требуется", "ищем" и подобного."""
    head = "\n".join(text.split("\n")[:6])
    for m in _TRIGGER.finditer(head):
        words: list[str] = []
        for w in m.group(1).split():
            lw = w.lower()
            if lw in _CUT_WORDS:
                break
            words.append(lw)
        if not words:
            continue
        if words[0] in _GENERIC_STOP or not re.search(r"[а-яё]{3}", words[0]):
            continue
        phrase = " ".join(words).strip("-")
        if len(phrase) >= 4:
            return phrase
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    first = re.sub(r"^(?:вакансия\s*:?\s*)", "", first.strip(), flags=re.IGNORECASE).strip("- ")
    if HIRING_RE.fullmatch(norm(first).strip(" !.:-")) or HIRING_RE.fullmatch(norm(first).strip(" !.:-").replace("на работу", "").strip() or "-"):
        return None
    if (
        first
        and len(first) <= 60
        and not re.search(r"\d|:|[.!?]$", first)
        and VACANCY_STRUCTURE_RE.search(norm(text))
        and 1 <= len(first.split()) <= 6
    ):
        return first.lower()
    return None


def _extend_generic(region: str, hit: ProfessionHit) -> str | None:
    """"Менеджер по страхованию", "специалист отдела продаж": общее слово из словаря плюс уточнение."""
    if hit.lemma not in ("менеджер", "специалист", "руководитель", "начальник"):
        return None
    m = re.match(
        r"[а-яё\-]+\s+(?:по|отдела)\s+[а-яё\-]+(?:\s+[а-яё\-]+){0,2}", region[hit.start:], re.IGNORECASE
    )
    if not m:
        return None
    words: list[str] = []
    for w in m.group().split():
        if w.lower() in _CUT_WORDS or (len(words) >= 3 and w.lower() in {"и", "в", "на"}):
            break
        if not re.search(r"[а-яё]", w.lower()):
            break
        words.append(w.lower())
    return " ".join(words) if len(words) >= 3 else None


def pick_profession(text: str) -> TitleChoice | None:
    """Должность вакансии: сначала строка с "требуется", потом первая строка, потом остальной текст."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None
    regions: list[str] = []
    for ln in lines[:6]:
        m = HIRING_RE.search(norm(ln))
        if m:
            regions.append(ln[m.end():])
            break
    regions.append(lines[0])
    regions.append("\n".join(lines[1:7]))
    cut = _SECTION_CUT.search(text)
    body = text[: cut.start()] if cut and cut.start() > 0 else text
    regions.append(body[:1500])
    generic_hit: ProfessionHit | None = None
    generic_region = ""
    for region in map(_scrub_meta, regions):
        hits = find_professions(region)
        good = _first_good(hits)
        if good:
            return TitleChoice(good.lemma, profession_category(good.lemma))
        if hits and generic_hit is None:
            generic_hit, generic_region = hits[0], region
    if generic_hit:
        extended = _extend_generic(generic_region, generic_hit)
        if extended:
            return TitleChoice(extended, "другое", generic=False, known=False)
        return TitleChoice(generic_hit.lemma, profession_category(generic_hit.lemma), generic=True)
    phrase = generic_title(text)
    if phrase and not HIRING_RE.search(norm(phrase)):  # "требуются сотрудники на базу" - объявление, а не должность
        return TitleChoice(clean_title_phrase(phrase) or phrase, "другое", generic=False, known=False)
    return None
