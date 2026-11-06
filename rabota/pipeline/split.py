"""Разбиение поста на несколько вакансий.

Три вида постов со списком должностей:
1. строки-пункты с маркером (эмодзи, дефис, нумерация "1."), у каждого свой кусок текста;
2. простой столбик строк после "требуются:" без маркеров;
3. перечень в одной строке: "требуются: бариста, повар", "Автомеханик / Автослесарь / Автомойщик".
Всё, что не принадлежит ни одному пункту (заголовок, адрес, телефон, график), попадает в `common`
и наследуется каждой вакансией.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rabota.pipeline.dictionaries import GENERIC_LEMMAS, HIRING_RE, find_professions, norm

_MARKER = re.compile(r"^(?:-|\d{1,2}\.)\s*")
_SECTION_HDR = re.compile(
    r"^(?:-\s*)?(?:мы\s+предлагаем|что\s+(?:мы\s+)?предлагаем|что\s+мы\s+предлагаем|условия|требования|обязанности|от\s+нас|"
    r"от\s+тебя|что\s+нужно\s+делать|контакты|наши\s+преимущества|преимущества|мы\s+гарантируем|ждем\s+от\s+вас|"
    r"мы\s+находимся|для\s+связи|звоните|подробная\s+информация)\b"
)
_SEPARATOR = re.compile(r"^\s*(?:,|/|;|\bи\b|\bили\b|\bтакже\b)\s*(?:также\s+)?$")
_LEAD_JUNK = re.compile(r"^[\s:\-]*")
# составные должности, найденные при разборе пунктов: название -> отрасль по словарю (или None)
_COMPOUND: dict[str, str | None] = {}


@dataclass
class Segment:
    """Кусок поста для одной вакансии."""

    text: str                       # собственный текст вакансии
    common: str = ""                # общий для поста текст (заголовок, адрес, телефон)
    profession: str | None = None   # должность из списка, если её дал сам разбор
    title: str = ""                 # строка-заголовок пункта
    lemmas_in_line: list[str] = field(default_factory=list)
    known: bool = True              # должность из словаря; False у составных ("специалист по отгрузке", "кондитер-пекарь")
    category: str | None = None     # отрасль по словарю, если известна (у составных должностей)

    @property
    def full(self) -> str:
        return (self.text + "\n" + self.common).strip() if self.common else self.text


def _split_marker(line: str) -> tuple[str, bool]:
    m = _MARKER.match(line)
    if m and m.end() > 0 and line[m.end():].strip():
        return line[m.end():].strip(), True
    return line.strip(), False


_QUALIFIER_CUT = re.compile(r"[(\[,;/:.!?]")
_QUALIFIED = re.compile(r"^(?:по|на|отдела)\s+[а-яё]", re.IGNORECASE)


def _compound(head: str, hits: list) -> tuple[str, str | None] | None:
    """Составная должность пункта: ("специалист по отгрузке", None) или ("кондитер-пекарь", "общепит").

    Нужна, чтобы пункты "специалист по отгрузке" и "специалист на пироги и пирожки" не слипались в одно
    "специалист", а "кондитер пекарь" не терялся за уже найденным "кондитер".
    """
    first = hits[0]
    rest = head[first.end:]
    if first.lemma in GENERIC_LEMMAS and _QUALIFIED.match(rest.lstrip()):
        tail = _QUALIFIER_CUT.split(rest, maxsplit=1)[0]
        words = tail.split()[:5]
        if len(words) >= 2:
            return f"{first.matched.lower()} {' '.join(words)}".lower(), None
    if (
        len(hits) > 1
        and hits[1].start - first.end <= 1
        and hits[1].lemma != first.lemma
        and first.lemma not in GENERIC_LEMMAS
        and hits[1].lemma not in GENERIC_LEMMAS
    ):
        cat = first.category if first.category != "другое" else None
        return f"{first.lemma}-{hits[1].lemma}", cat
    return None


def _line_items(head: str) -> list[str]:
    """Должности в начале строки-пункта: "Слесарей/электриков" даёт слесарь и электрик."""
    if not head or len(head) > 110 or len(head.split()) > 14:
        return []
    hits = find_professions(head)
    if not hits or hits[0].start > 0:
        return []
    compound = _compound(head, hits)
    if compound:
        _COMPOUND[compound[0]] = compound[1]
        return [compound[0]]
    lemmas = [hits[0].lemma]
    prev_end = hits[0].end
    for h in hits[1:]:
        if _SEPARATOR.match(head[prev_end:h.start]):
            lemmas.append(h.lemma)
            prev_end = h.end
        else:
            break
    return lemmas


def _segment(text: str, common: str, profession: str, title: str, lemmas: list[str]) -> Segment:
    """Сегмент одной вакансии; составная должность помечается как не из словаря, отрасль берётся от первого слова."""
    if profession in _COMPOUND:
        return Segment(text=text, common=common, profession=profession, title=title, lemmas_in_line=lemmas,
                       known=False, category=_COMPOUND[profession])
    return Segment(text=text, common=common, profession=profession, title=title, lemmas_in_line=lemmas)


def _blank(line: str) -> bool:
    return not line.strip()


def _unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


@dataclass
class _Start:
    idx: int
    lemmas: list[str]
    head: str


def _find_starts(lines: list[str]) -> list[_Start]:
    starts: list[_Start] = []
    in_section = False
    plain_zone = False
    for i, line in enumerate(lines):
        if _blank(line):
            in_section = False
            plain_zone = False
            continue
        head, marked = _split_marker(line)
        if _SECTION_HDR.match(norm(line)):
            in_section = True
            plain_zone = False
            continue
        if not in_section:
            eligible = marked or plain_zone
            if eligible:
                lemmas = _line_items(head)
                if lemmas:
                    starts.append(_Start(i, lemmas, head))
            if not marked and line.rstrip().endswith(":"):
                plain_zone = True
    return starts


def _inline_list(lines: list[str]) -> list[str]:
    """Перечень должностей в строке с "требуются": бариста, повар / официант."""
    for line in lines[:6]:
        low = norm(line)
        m = HIRING_RE.search(low)
        if not m:
            continue
        rest = line[m.end():]
        lead = _LEAD_JUNK.match(rest)
        rest = rest[lead.end():] if lead else rest
        colon = rest.find(":", 0, 70)
        if colon > 0 and not find_professions(rest[:colon]):
            rest = rest[colon + 1:].lstrip()
        hits = find_professions(rest)
        if not hits:
            continue
        # допускаем пару слов перед первой должностью ("будут", "срочно")
        if hits[0].start > 14 or re.search(r"\b(?:в|на|для|с|со|по)\b", norm(rest[:hits[0].start])):
            continue
        seq = [hits[0]]
        for h in hits[1:]:
            if _SEPARATOR.match(rest[seq[-1].end:h.start]):
                seq.append(h)
            else:
                break
        lemmas = _unique([h.lemma for h in seq if h.lemma not in GENERIC_LEMMAS])
        if len(lemmas) >= 2:
            return lemmas
    return []


_TRIGGER_PREFIX = re.compile(
    r"^\W*(?:срочно\W*)?(?:вакансия\W*)?(?:требу(?:ется|ются)|нужны|нужен|нужна|ищем|открыта\s+вакансия)\W*", re.IGNORECASE
)


def _paragraph_lemmas(par: list[str]) -> list[str]:
    """Должность в начале абзаца: "Требуется пекарь...", "Повар в заготовочный цех от 5000 выход"."""
    for line in par[:3]:
        head, _ = _split_marker(line.strip())
        rest = _TRIGGER_PREFIX.sub("", head)
        lemmas = _line_items(rest)
        if lemmas:
            return lemmas
    return []


def _paragraph_segments(lines: list[str]) -> list[Segment]:
    """Абзацы, каждый из которых открывается своей должностью (в том числе строкой "Требуется ...")."""
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if _blank(line):
            if paragraphs[-1]:
                paragraphs.append([])
        else:
            paragraphs[-1].append(line)
    paragraphs = [p for p in paragraphs if p]
    found = [(p, _paragraph_lemmas(p)) for p in paragraphs]
    lemmas_all = _unique([lm for _, lms in found for lm in lms])
    if len([1 for _, lms in found if lms]) < 2 or len(lemmas_all) < 2:
        return []
    common = "\n\n".join("\n".join(p) for p, lms in found if not lms)
    segments: list[Segment] = []
    seen: set[str] = set()
    for par, lms in found:
        for lm in lms:
            if lm in seen:
                continue
            seen.add(lm)
            segments.append(_segment("\n".join(par), common, lm, par[0].strip("- "), list(lms)))
    return segments


def _header_lemma(lines: list[str], first_start: int) -> str | None:
    """Должность в заголовке поста до первого пункта ("Срочно! Требуются повара!")."""
    for line in lines[:first_start][:4]:
        m = HIRING_RE.search(norm(line))
        if not m:
            continue
        for h in find_professions(line[m.end():]):
            if h.lemma not in GENERIC_LEMMAS:
                return h.lemma
    return None


def split_post(text: str) -> list[Segment]:
    """Разбить очищенный текст на сегменты по вакансиям. Один сегмент, если список должностей не найден."""
    text = text.strip()
    if not text:
        return []
    lines = text.split("\n")
    starts = _find_starts(lines)
    lemmas_all = _unique([lm for s in starts for lm in s.lemmas])
    header = _header_lemma(lines, starts[0].idx) if starts else None
    if starts and header and header not in lemmas_all:
        lemmas_all.append(header)
    if len(lemmas_all) >= 2 and starts:
        return _segments_from_starts(lines, starts, header)
    inline = _inline_list(lines)
    if inline:
        return [Segment(text=text, common="", profession=lm, title=lm, lemmas_in_line=inline) for lm in inline]
    paragraphs = _paragraph_segments(lines)
    if paragraphs:
        return paragraphs
    return [Segment(text=text)]


def _segments_from_starts(lines: list[str], starts: list[_Start], header: str | None) -> list[Segment]:
    ranges: list[tuple[int, int, _Start]] = []
    for k, s in enumerate(starts):
        if k + 1 < len(starts):
            end = starts[k + 1].idx
        else:
            end = len(lines)
            for j in range(s.idx + 1, len(lines)):
                if _blank(lines[j]) or _SECTION_HDR.match(norm(lines[j])):
                    end = j
                    break
        ranges.append((s.idx, end, s))
    claimed: set[int] = set()
    for a, b, _ in ranges:
        claimed.update(range(a, b))
    common_lines = [ln for i, ln in enumerate(lines) if i not in claimed]
    common = re.sub(r"\n{3,}", "\n\n", "\n".join(common_lines)).strip()

    segments: list[Segment] = []
    seen: set[str] = set()
    for a, b, s in ranges:
        own = "\n".join(lines[a:b]).strip()
        for lemma in s.lemmas:
            if lemma in seen:
                continue
            seen.add(lemma)
            segments.append(_segment(own, common, lemma, s.head, list(s.lemmas)))
    if header and header not in seen:
        header_line = next((ln for ln in lines[: starts[0].idx] if HIRING_RE.search(norm(ln))), "")
        segments.insert(0, Segment(text=header_line.strip(), common=common, profession=header, title=header_line.strip()))
    return segments
