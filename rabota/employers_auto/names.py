"""Название работодателя: нормализация для сравнения, правовая форма и отсев того, что карточкой быть не должно.

    info = parse_name("ООО «Рога и Копыта»")      -> core "рога и копыта", legal_form "ООО"
    decide("Частное лицо")                        -> Decision(ok=False, reason="placeholder")

Правила отсева (карточку не заводим):
- пустое имя и заглушки («без названия», «частное лицо», «работодатель», «не указан»);
- слишком короткое имя (меньше трёх букв);
- имя, похожее на ФИО человека, и ИП на ФИО: публичной карточкой делаем только организацию;
- имя совпадает с профессией («бариста», «повар»);
- мусор из разбора: телефон, почта, ссылка, призыв («требуется»), слишком длинная строка.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rabota.pipeline.dictionaries import find_professions
from rabota.pipeline.homoglyphs import fix_homoglyphs

# греческие буквы-двойники, которых нет в общей таблице конвейера
_EXTRA_HOMOGLYPHS = str.maketrans({"Π": "П", "π": "п", "Δ": "Д", "Λ": "Л", "Γ": "Г", "Φ": "Ф", "Σ": "Е", "Ω": "О"})

# Правовые формы и организационные приставки: отдельное поле legal_form, из сравнения выбрасываются.
LEGAL_FORMS = (
    "ооо", "оао", "зао", "пао", "ао", "нао", "ип", "чп", "гуп", "муп", "фгуп", "фгбу", "фгбоу", "фгкоу", "фгку",
    "гбу", "гку", "гау", "мбу", "мку", "мау", "гбуз", "гбоу", "гбдоу", "гбусо", "гбпоу", "мбоу", "мбдоу", "моу", "гоу",
    "гпоу", "гбук", "крфх", "кфх", "тсж", "спк", "нко", "ано", "одо", "тоо", "пк", "ук", "ооо ук", "ркц", "цгб", "црб",
)
_FORM_SET = frozenset(LEGAL_FORMS)
_LONG_FORMS = (
    (re.compile(r"общество\s+с\s+ограниченной\s+ответственностью"), "ооо"),
    (re.compile(r"индивидуальный\s+предприниматель"), "ип"),
    (re.compile(r"акционерное\s+общество"), "ао"),
    (re.compile(r"публичное\s+акционерное\s+общество"), "пао"),
    (re.compile(r"государственное\s+унитарное\s+предприятие"), "гуп"),
    (re.compile(r"муниципальное\s+унитарное\s+предприятие"), "муп"),
    (re.compile(r"частное\s+предприятие"), "чп"),
)
# слова, которые не отличают одного работодателя от другого
_NOISE = frozenset({"днр", "лнр", "рф", "г", "гор", "город"})

PLACEHOLDERS = frozenset({
    "без названия", "безымянный", "частное лицо", "частный", "частный работодатель", "физлицо", "физическое лицо",
    "работодатель", "прямой работодатель", "организация", "компания", "фирма", "предприятие", "не указан", "не указано",
    "неизвестно", "неизвестный", "аноним", "анонимно", "нет", "отсутствует", "не определен", "скрыт", "скрыто",
    "имя скрыто", "hr", "hr менеджер", "кадры", "отдел кадров", "администрация сайта", "вакансия", "вакансии", "работа",
    "подработка", "объявление", "заказчик", "хозяин", "собственник", "владелец", "директор", "менеджер", "и",
})
# общие слова без названия: «кафе», «аптека» сами по себе не работодатель
GENERIC = frozenset({
    "кафе", "ресторан", "магазин", "аптека", "салон", "салон красоты", "парикмахерская", "школа", "детский сад",
    "больница", "поликлиника", "столовая", "пекарня", "кофейня", "бар", "клуб", "отель", "гостиница", "склад",
    "офис", "завод", "фабрика", "цех", "шиномонтаж", "автосервис", "сто", "стройка", "производство", "торговая сеть",
    "сеть магазинов", "сеть кафе", "продуктовый магазин", "минимаркет", "супермаркет", "рынок", "ферма",
})
_BAD_WORDS = re.compile(
    r"требу|ищем|ищу|нужн|приглаш|набира|вакан|принимаем|оплата|зарплат|график|звоните|пишите|подработк|срочно|"
    r"сотрудник|обязанност|условия|резюме|оформлен"
)
_CONTACT = re.compile(r"@|https?:|www\.|t\.me|\.ru\b|\.com\b|\.рф\b|\d{5,}|\+\d")

FIRST_NAMES = frozenset(["александр", "александра", "алексей", "алена", "алина", "алиса", "алла", "анастасия", "анатолий", "андрей", "анжела", "анна", "антон", "антонина", "аркадий", "артем", "артём", "артур", "богдан", "борис", "вадим", "валентин", "валентина", "валерий", "валерия", "вера", "вероника", "виктор", "виктория", "виталий", "владимир", "владислав", "вячеслав", "галина", "геннадий", "георгий", "глеб", "григорий", "данил", "даниил", "дарья", "денис", "дмитрий", "евгений", "евгения", "екатерина", "елена", "елизавета", "жанна", "зинаида", "зоя", "иван", "игорь", "инна", "ирина", "карина", "кирилл", "клавдия", "константин", "кристина", "ксения", "лариса", "леонид", "лидия", "лилия", "любовь", "людмила", "максим", "маргарита", "марина", "мария", "марк", "михаил", "надежда", "наталья", "наталия", "никита", "николай", "нина", "олег", "олеся", "ольга", "павел", "полина", "раиса", "роман", "ростислав", "руслан", "светлана", "святослав", "семен", "семён", "сергей", "снежана", "софия", "софья", "станислав", "степан", "тамара", "татьяна", "тимофей", "тимур", "ульяна", "фёдор", "федор", "филипп", "эдуард", "элина", "эльвира", "эмма", "юлия", "юрий", "яна", "ярослав", "ахмед", "рустам", "эльдар", "заур", "магомед", "мурат", "залина", "мустафа", "расул", "ислам", "салават", "султан", "тагир", "аслан"])
_SURNAME_END = re.compile(
    r"(?:ов|ова|ев|ева|ёв|ёва|ин|ина|ын|ына|ский|ская|цкий|цкая|ской|ко|енко|ук|юк|чук|ян|янц|дзе|швили|ов|ий|ая)$"
)
_PATRONYMIC = re.compile(r"(?:ович|евич|ьич|овна|евна|ёвна|ична|инична|ыч|оглы|кызы|гызы|улы)$")
_INITIAL = re.compile(r"^[а-яa-z]\.?$")
_WORD = re.compile(r"[а-яa-z0-9]+(?:-[а-яa-z0-9]+)*")


@dataclass(frozen=True)
class NameInfo:
    display: str        # как показываем: без мусора, правовая форма остаётся
    core: str           # без правовой формы, регистра, кавычек и «ё»
    legal_form: str     # ООО, ГУП, ... заглавными; пусто, если не нашли
    norm: str           # ключ сравнения (core без шума)


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str         # ok или причина отказа
    info: NameInfo | None = None


REASONS = {
    "ok": "подходит",
    "empty": "пустое название",
    "placeholder": "заглушка вместо названия",
    "short": "слишком короткое название",
    "person": "похоже на ФИО человека",
    "profession": "название совпадает с профессией",
    "generic": "слишком общее слово без названия",
    "garbage": "в названии телефон, ссылка или текст объявления",
    "long": "слишком длинная строка",
}


def fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def clean_display(name: str) -> str:
    """Название для показа: буквы-двойники, пробелы, одинаковые ёлочки, без висячих кавычек."""
    text = fix_homoglyphs((name or "").translate(_EXTRA_HOMOGLYPHS))
    text = re.sub(r"\s+", " ", text.replace(" ", " ")).strip(" \t,;:.-")
    out: list[str] = []
    opened = False
    for ch in text:
        if ch in '"“”„':
            out.append("»" if opened else "«")
            opened = not opened
        else:
            out.append(ch)
    text = "".join(out)
    text = re.sub(r"«\s+", "«", text)
    text = re.sub(r"\s+»", "»", text)
    text = re.sub(r"»(?=[\w«])", "» ", text)
    text = re.sub(r"(?<=\w)«", " «", text)
    if text.count("«") != text.count("»"):
        # висячая кавычка из кривого разбора: убираем лишнюю с края
        if text.endswith("«"):
            text = text[:-1].rstrip()
        elif text.startswith("»"):
            text = text[1:].lstrip()
        if text.count("«") != text.count("»"):
            text = text.replace("«", "").replace("»", "")
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def parse_name(name: str) -> NameInfo:
    """Разобрать название: показываемое, ядро, правовая форма, ключ сравнения."""
    display = clean_display(name)
    low = fold(fix_homoglyphs(display.translate(_EXTRA_HOMOGLYPHS)))
    for pattern, short in _LONG_FORMS:
        low = pattern.sub(f" {short} ", low)
    tokens = [t for t in _tokens(low) if t]
    forms: list[str] = []
    # приставки в начале: «ооо ук», «гуп днр», «фгбоу во»
    while tokens and (tokens[0] in _FORM_SET or tokens[0] in _NOISE or (tokens[0] == "во" and forms)):
        head = tokens.pop(0)
        if head in _FORM_SET:
            forms.append(head)
    # хвост: «Альфа, ООО»
    while tokens and tokens[-1] in _FORM_SET and len(tokens) > 1:
        forms.append(tokens.pop())
    core_tokens = [t for t in tokens if t not in _NOISE]
    core = " ".join(core_tokens)
    norm = " ".join(t.replace("-", " ") for t in core_tokens).strip()
    legal = " ".join(f.upper() for f in dict.fromkeys(forms))
    return NameInfo(display=display, core=core, legal_form=legal, norm=norm)


def looks_like_person(name: str, legal_form: str = "") -> bool:
    """Похоже на ФИО: три слова с отчеством, фамилия и имя, «Иван И.», одиночное имя."""
    raw = clean_display(name)
    info_tokens = _tokens(raw)
    # правовые формы не часть ФИО: «ИП Иванов Иван»
    words = [t for t in info_tokens if t not in _FORM_SET and t not in _NOISE]
    if not words or len(words) > 4:
        return False
    initials = [w for w in words if _INITIAL.match(w)]
    if initials and len(words) <= 3 and len(initials) < len(words):
        rest = [w for w in words if w not in initials]
        if all(len(w) > 2 for w in rest):
            return True  # «Иван И.», «И. И. Иванов»
    cyr_words = [w for w in words if re.fullmatch(r"[а-я]+(?:-[а-я]+)?", w)]
    if len(cyr_words) != len(words):
        return False
    if any(_PATRONYMIC.search(w) for w in words) and len(words) >= 2:
        return True
    if len(words) == 1:
        return words[0] in FIRST_NAMES
    firsts = [w for w in words if w in FIRST_NAMES]
    surnames = [w for w in words if _SURNAME_END.search(w) and w not in FIRST_NAMES]
    if len(words) in (2, 3) and firsts and surnames:
        return True
    return len(words) == 2 and all(w in FIRST_NAMES for w in words)


def is_profession(core: str, profession: str | None = None) -> bool:
    if not core:
        return False
    if profession and fold(profession).strip() == core:
        return True
    hits = find_professions(core)
    return bool(hits) and hits[0].start <= 1 and hits[0].end >= len(core) - 1


def decide(name: str | None, profession: str | None = None, *, source_type: str = "") -> Decision:
    """Можно ли заводить карточку для этого названия. Всегда возвращает разбор, если он получился."""
    text = (name or "").strip()
    if not text:
        return Decision(False, "empty")
    info = parse_name(text)
    low = fold(info.display)
    if low in PLACEHOLDERS or info.core in PLACEHOLDERS or low.strip(" .-") in PLACEHOLDERS:
        return Decision(False, "placeholder", info)
    if len(info.display) > 90 or len(info.core.split()) > 9:
        return Decision(False, "long", info)
    if _CONTACT.search(info.display) or _BAD_WORDS.search(low):
        return Decision(False, "garbage", info)
    letters = re.sub(r"[^a-zа-я]", "", info.core)
    if len(letters) < 3:
        return Decision(False, "short" if info.core else "empty", info)
    if info.core in GENERIC or info.norm in GENERIC:
        return Decision(False, "generic", info)
    if is_profession(info.core, profession):
        return Decision(False, "profession", info)
    if looks_like_person(info.display):
        return Decision(False, "person", info)
    return Decision(True, "ok", info)


def city_key(city: str | None) -> str:
    return fold(city or "").strip()
