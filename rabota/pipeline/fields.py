"""Поля вакансии из текста: график, город, район, опыт, возраст, занятость, формат, работодатель, требования."""
from __future__ import annotations

import re

from rabota.pipeline.dictionaries import (
    CITY_DISPLAY,
    CITY_PATTERNS,
    CONTEXT_CATEGORY_RULES,
    DISTRICT_PATTERNS,
    DISTRICTS_NEED_WORD,
    LANDMARK_PATTERNS,
    LANDMARKS_NEED_WORD,
    SCAM_RE,
    norm,
)

_LB = r"(?<![а-яa-z0-9])"
_RB = r"(?![а-яa-z0-9])"

# ---------------------------------------------------------------- график

_RATIO_OK = {
    (5, 2), (2, 2), (3, 3), (6, 1), (7, 7), (4, 3), (4, 2), (1, 3), (1, 2), (2, 1), (14, 14), (15, 15), (7, 14),
    (21, 7), (30, 30), (60, 60), (45, 15), (6, 2), (3, 1), (2, 3), (1, 1), (5, 1), (4, 4), (10, 10), (20, 10),
}
_RATIO = re.compile(r"(?<![\d/.:])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/.:])")
_RATIO_WORDS = re.compile(r"(?<![а-я0-9])(\d{1,2})\s+через\s+(\d{1,2})(?![а-я0-9])")
_HOURS_S = re.compile(
    r"с\s*(\d{1,2})(?:[:.\-](\d{2}))?\s*(?:до|-|по)\s*(\d{1,2})(?:[:.\-](\d{2}))?(?![\d:])"
)
_HOURS_BARE = re.compile(r"(?<![\d:.])(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})(?![\d:])")


def _hhmm(h: str, m: str | None) -> str:
    return f"{int(h)}:{m or '00'}"


def parse_schedule(text: str) -> str | None:
    """5/2, 2/2, 3/3, сутки, свободный, "с 8:30 до 17:00"."""
    low = norm(text)
    for rx in (_RATIO,):
        for m in rx.finditer(low):
            a, b = int(m.group(1)), int(m.group(2))
            if (a, b) == (24, 3):
                return "сутки"
            if (a, b) in _RATIO_OK:
                return f"{a}/{b}"
    if re.search(r"\b24\s*/\s*(?:3|48|72)\b", low):
        return "сутки"
    m = _RATIO_WORDS.search(low)
    if m and (int(m.group(1)), int(m.group(2))) in _RATIO_OK:
        return f"{int(m.group(1))}/{int(m.group(2))}"
    if re.search(r"сутки\s*/\s*трое|посуточ|сутки\s+через|суточный\s+график|сутки\s+(?:через\s+)?(?:двое|трое)|\bсутки\b", low):
        return "сутки"
    if re.search(r"\bпн\s*-\s*пт\b|понедельник\s*-\s*пятница|по\s+будням|будни\b|пятидневн", low):
        return "5/2"
    if re.search(r"\bсвободный\s+график|\bгибкий\s+график|плавающий\s+график|график\s+(?:свободный|гибкий|плавающий)|график\s+выбираете", low):
        return "свободный"
    for m in _HOURS_S.finditer(low):
        h1, h2 = int(m.group(1)), int(m.group(3))
        if h1 < 24 and h2 <= 24 and (m.group(2) or m.group(4) or h2 >= 5):
            if m.group(2) is None and m.group(4) is None and not (h1 <= 12 and h2 <= 24):
                continue
            return f"с {_hhmm(m.group(1), m.group(2))} до {_hhmm(m.group(3), m.group(4))}"
    m = _HOURS_BARE.search(low)
    if m and int(m.group(1)) < 24 and int(m.group(3)) <= 24:
        return f"с {_hhmm(m.group(1), m.group(2))} до {_hhmm(m.group(3), m.group(4))}"
    return None


# ---------------------------------------------------------------- город и район

_CITY_RX = {name: re.compile(_LB + pat + _RB) for name, pat in CITY_PATTERNS.items()}
_DISTRICT_WORD = r"(?:район\w*|р-н\w*|р-он\w*|р-ны)"
_DISTRICT_RX = {name: re.compile(_LB + pat + _RB) for name, pat in DISTRICT_PATTERNS.items()}
_LANDMARK_RX = {name: re.compile(_LB + pat + _RB) for name, pat in LANDMARK_PATTERNS.items()}
_STREET_AFTER = re.compile(r"^[\s\-]*(?:проспект|пр-т|просп|бульвар|б-р|улиц|ул\.?|площад|пл\.|шоссе|переул|пер\.)")
_DISTRICT_WORD_NEAR = re.compile(r"^[\s,.\-]*(?:и\s+\w+\s+)?" + _DISTRICT_WORD)
_DISTRICT_WORD_BEFORE = re.compile(_DISTRICT_WORD + r"[\s,.:\-]*(?:г\.\s*\w+\s*,?\s*)?$")


def parse_city(text: str) -> str | None:
    """Первый упомянутый город. Если города нет, но указан район Донецка, то Донецк."""
    low = norm(text)
    best: tuple[int, str] | None = None
    for name, rx in _CITY_RX.items():
        m = rx.search(low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best:
        return CITY_DISPLAY.get(best[1], best[1])
    # район Донецка без названия города
    if parse_district(text, allow_landmark=False):
        return "Донецк"
    return None


def _find_formal_district(low: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for name, rx in _DISTRICT_RX.items():
        for m in rx.finditer(low):
            after = low[m.end(): m.end() + 40]
            before = low[max(0, m.start() - 30): m.start()]
            near = bool(_DISTRICT_WORD_NEAR.match(after) or _DISTRICT_WORD_BEFORE.search(before))
            if name in DISTRICTS_NEED_WORD and not near:
                continue
            if not near and _STREET_AFTER.match(after):
                continue
            # без слова "район": "Ворошиловский", но не фамилия и не название улицы
            if (
                not near
                and name not in DISTRICTS_NEED_WORD
                and re.match(r"^[\s\-]*(?:проспект|пр-т|ул|улица|бульвар)", after)
            ):
                continue
            found.append((m.start(), name))
            break
    return sorted(found)


_CENTER = re.compile(
    _LB + r"(?:центр(?:е|а)?\s+(?:г\.\s*)?(?:донецк|макеевк|горловк)\w*|в\s+(?:самом\s+)?центре\s+(?:города|донецк\w*|макеевк\w*)|"
    r"(?:донецк|макеевк|горловк)\w*\s*[,(]\s*центр(?:е)?" + _RB + ")"
)


def parse_district(text: str, allow_landmark: bool = True) -> str | None:
    """Район города: Ворошиловский, Калининский, Текстильщик, МИСИ."""
    low = norm(text)
    formal = _find_formal_district(low)
    if formal:
        return formal[0][1]
    if not allow_landmark:
        return None
    marks: list[tuple[int, str]] = []
    for name, rx in _LANDMARK_RX.items():
        m = rx.search(low)
        if not m:
            continue
        if name in LANDMARKS_NEED_WORD:
            before = low[max(0, m.start() - 25): m.start()]
            if not re.search(r"(?:район\w*|р-н\w*|р-не|мкр\.?|микрорайон\w*|в)\s*$", before):
                continue
        marks.append((m.start(), name))
    if marks:
        return min(marks)[1]
    if _CENTER.search(low):
        return "Центр"
    return None


# ---------------------------------------------------------------- опыт и возраст

def parse_experience(text: str) -> str | None:
    low = norm(text)
    m = re.search(
        r"опыт\w*(?:\s+работы)?[:\s-]*(?:не\s+менее|от|минимум|более|свыше)?\s*(\d+)(?:\s*(?:-|до)\s*\d+)?\s*(?:лет|года|год|г\b)", low
    )
    if m:
        n = int(m.group(1))
        return "от 1 года" if n == 1 else f"от {n} лет"
    m = re.search(r"опыт\w*(?:\s+работы)?\s*(?:не\s+менее|от)?\s*(\d+)\s*(?:мес|месяц)", low)
    if m:
        n = int(m.group(1))
        return f"от {n} мес."
    m = re.search(r"стаж\s+(?:от\s+)?(\d+)\s*(?:лет|года|год)", low)
    if m:
        n = int(m.group(1))
        return "от 1 года" if n == 1 else f"от {n} лет"
    if re.search(r"с\s+опытом\s+или\s+без|опыт\s+(?:не\s+)?(?:обязателен|важен)|опыт\s+не\s+обязательн", low):
        return "не обязателен"
    if re.search(
        r"без\s+опыта|опыт\s+не\s+(?:требуется|нужен|необходим|важен)|не\s+требуется\s+опыт|можно\s+без\s+опыта|"
        r"опыт\s+работы\s+не\s+(?:требуется|нужен)|обучим|обучение\s+(?:предусмотрено|за\s+счет|проводится)|"
        r"возьмем\s+без\s+опыта|без\s+опыта\s+работы|обучаем|научим|даже\s+если\s+у\s+вас\s+нет\s+опыта",
        low,
    ):
        return "не нужен"
    if re.search(r"опыт\w*[^.\n]{0,40}\(?\s*желательн|желателен\s+опыт|опыт\s+желателен|опыт\w*\s+будет\s+плюсом|опыт\w*\s+приветствуется", low):
        return "желателен"
    if re.search(r"с\s+опытом\b|опыт\s+работы\b|опыт\s+обязателен|наличие\s+опыта|опыт\s+от\b|опытн", low):
        return "нужен"
    return None


def parse_age_min(text: str) -> int | None:
    low = norm(text)
    m = re.search(r"(?:возраст\w*|от)\s*(?:кандидат\w*\s*)?(?:от\s*)?(\d{2})\s*(?:-\s*\d{2}\s*)?(?:лет|года|\+)", low)
    if m and 16 <= int(m.group(1)) <= 60:
        return int(m.group(1))
    m = re.search(r"(?<![\d.,:/-])(\d{2})\s*\+(?!\d)", low)
    if m and 16 <= int(m.group(1)) <= 25:
        return int(m.group(1))
    m = re.search(r"\(\s*(\d{2})\s*лет\s*\)|(?:от|с)\s+(\d{2})\s+(?:лет|года)", low)
    if m:
        v = int(m.group(1) or m.group(2))
        if 16 <= v <= 60:
            return v
    m = re.search(r"(?:муж\w*|жен\w*|девушк\w*|парн\w*|мужчин\w*|женщин\w*)[^\d\n]{0,10}(\d{2})\s*-\s*\d{2}\s*лет", low)
    if m and 16 <= int(m.group(1)) <= 60:
        return int(m.group(1))
    if re.search(r"совершеннолетн|совершенолетн|только\s+18|от\s+18", low):
        return 18
    return None


# ---------------------------------------------------------------- занятость и формат

def parse_employment(text: str, schedule: str | None = None) -> str | None:
    low = norm(text)
    if re.search(r"на\s+(?:сегодня|завтра|послезавтра)|на\s+\d+\s+(?:человек|числа|октября|ноября|декабря)|на\s+один\s+день|(?<![а-я])разов\w+|одноразов\w+|на\s+1\s+день|на\s+\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|на\s+\d+\s+дн", low):
        return "на сегодня"
    if re.search(r"подработк|частичн\w+\s+занятост|неполный\s+(?:рабочий\s+)?день|совмещени", low):
        return "подработка"
    if re.search(r"посменн|сменн\w+\s+(?:график|работа)|занятость\s*:\s*сменн", low) or (schedule and re.match(r"(?:\d{1,2}/\d{1,2}|сутки)", schedule) and schedule != "5/2" and schedule not in ("6/1",)):
        return "сменная"
    if re.search(r"постоянн\w+\s+(?:работ|основ|место)|на\s+постоянн|официальн\w+\s+(?:трудоустройств|оформлен)|полная\s+занятость|занятость\s*:\s*полная|трудовой\s+договор|по\s+тк\s+рф|полный\s+(?:рабочий\s+)?день", low):
        return "постоянно"
    if schedule == "5/2":
        return "постоянно"
    return None


def detect_context_category(text: str) -> str | None:
    low = norm(text)
    for category, rx in CONTEXT_CATEGORY_RULES:
        if re.search(rx, low):
            return category
    return None


def parse_work_format(text: str, category: str | None = None) -> str | None:
    low = norm(text)
    if re.search(r"удален\w+|на\s+дому|из\s+дома|дистанционн|онлайн-работ|работа\s+онлайн|формат\s*:[^\n]*удален", low):
        return "удалённо"
    if re.search(r"вахт", low):
        return "вахта"
    explicit = re.search(r"формат\s*:\s*([^\n]+)", low)
    if explicit:
        val = explicit.group(1)
        if "разъезд" in val or "выездн" in val:
            return "выезд"
        if "удален" in val:
            return "удалённо"
        if "вахт" in val:
            return "вахта"
    if re.search(r"разъездн|выездн|развоз\s+(?:товар|продукт|лви|напитк)|по\s+торговым\s+точкам|курьер|торговы\w+\s+представител|экспедитор|доставк\w+\s+товар", low):
        return "выезд"
    mapping = {
        "склад": "склад", "стройка": "объект", "транспорт": "выезд", "офис": "офис", "общепит": "точка",
        "торговля": "точка", "услуги": "точка", "медицина": "точка", "образование": "точка",
    }
    if re.search(r"на\s+объект|стройк|строительн\w+\s+объект|демонтаж|монтаж", low) and category in (None, "стройка", "другое"):
        return "объект"
    if category in mapping:
        return mapping[category]
    if re.search(r"склад", low):
        return "склад"
    if re.search(r"\bофис", low):
        return "офис"
    if re.search(r"магазин|кафе|ресторан|кофейн|зал\b|салон|точк", low):
        return "точка"
    return None


# ---------------------------------------------------------------- работодатель и требования

_QUOTED = r"[«\"“]\s*([^»\"”\n]{2,40}?)\s*[»\"”]"
_EMPLOYER_TYPES = (
    r"(?:ООО|ОАО|ЗАО|ГБУЗ|ГУП|ФГБОУ\s+ВО|ТК|ТМ|ИП|кафе|ресторан|ресторан-поместье|магазин|салон\s+красоты|салон|фастфуд|"
    r"кофейн[яю]|пекарн[яю]|сети?|торговой?\s+сети?|хлебозавод|компани[яи]|фабрик[аи]|студи[яи]|барбершоп|клиник[аи]|аптек[аи]|"
    r"гостиниц[аы]|отел[ья]|предприяти[ея]|кондитерск\w+\s+фабрик\w+|кулинарн\w+\s+лавк\w+|парикмахерск\w+|фуд-корт|сеть)"
)
_EMPLOYER_QUOTED = re.compile(r"(?i:" + _EMPLOYER_TYPES + r")\s*" + _QUOTED)
_EMPLOYER_LABEL = re.compile(r"(?im)^\W*(?:компания|работодатель|организация)\s*:\s*([^\n]{2,100})$")
_EMPLOYER_LATIN = re.compile(r"^\W*((?:[A-Z][A-Za-z0-9&]*)(?:\s+[A-Z][A-Za-z0-9&]*){0,2})\s+(?:ищет|приглашает|открывает|набирает|в\s+поиск)", re.MULTILINE)


def _tidy_employer(name: str) -> str:
    name = name.strip(" .,:;")
    name = re.sub(r'"([^"]+)"', r"«\1»", name)
    name = name.strip("\"«»")
    if name.isupper() and len(name) > 4:
        name = name.capitalize()
    return name


def parse_employer(text: str) -> str | None:
    m = _EMPLOYER_LABEL.search(text)
    if m:
        return _tidy_employer(m.group(1))
    m = _EMPLOYER_QUOTED.search(text)
    if m:
        return _tidy_employer(m.group(1))
    m = _EMPLOYER_LATIN.search(text)
    if m:
        return _tidy_employer(m.group(1))
    return None


_SECTION_START = re.compile(r"(?im)^\W*(?:требования(?:\s+к\s+\w+)?|мы\s+ищем|что\s+нужно|от\s+вас|от\s+тебя|кого\s+ищем)\s*:?\s*$")
_REQ_INLINE = re.compile(r"(?i)^\W*требования\s*:\s*(\S.+)$")
_SECTION_STOP = re.compile(r"(?im)^\W*(?:мы\s+предлагаем|условия|обязанности|что\s+(?:мы\s+)?предлагаем|от\s+нас|контакты|график|оплата)\b")
_REQ_KEYWORDS = (
    (r"мед(?:\.|ицинск\w+)\s*книжк", "медицинская книжка"),
    (r"(?:водительск\w+\s+(?:удостоверени\w+|прав\w+)|права\s+категори\w+|категори\w+\s+[abcdeвсде]\b)", "водительские права"),
    (r"\b1[сc]\b", "1С"),
    (r"высшее\s+образовани|высшее\s+техническ|высшее\s+медицинск", "высшее образование"),
    (r"среднее\s+специальн|среднее\s+профессиональн|профильное\s+образован", "профильное образование"),
    (r"аккредитаци", "аккредитация"),
    (r"допуск\w*\s+(?:к\s+работе\s+)?(?:до|к)?\s*\d*\s*в\b|электробезопасност", "допуск по электробезопасности"),
    (r"удостоверени\w+\s+(?:тракторист|крановщик|стропальщик)|квалификационн\w+\s+разряд|\d-?\d?\s*разряд", "разряд"),
    (r"паспорт\s+рф|гражданств\w+\s+рф", "паспорт РФ"),
)


def parse_requirements(text: str) -> str | None:
    """Блок "Требования" строкой через точку с запятой, а при его отсутствии признаки из текста."""
    lines = text.split("\n")
    items: list[str] = []
    in_block = False
    for line in lines:
        inline = _REQ_INLINE.match(line)
        if inline and not items:
            items.append(inline.group(1).strip()[:160].rstrip(";.,"))
            break
        if _SECTION_START.match(line):
            in_block = True
            continue
        if in_block:
            if not line.strip() or _SECTION_STOP.match(line):
                break
            item = re.sub(r"^[-*\s]+", "", line).strip().rstrip(";.,")
            if item:
                items.append(item[:80])
            if len(items) >= 5:
                break
    if items:
        return "; ".join(items)
    low = norm(text)
    found: list[str] = []
    for rx, label in _REQ_KEYWORDS:
        if re.search(rx, low) and label not in found:
            found.append(label)
    return ", ".join(found) if found else None


# ---------------------------------------------------------------- подозрительные признаки

def detect_suspicious(text: str, pay_max: int | None = None, pay_period: str | None = None) -> bool:
    """Признаки мнимой вакансии: закладки, предоплата, ставки, оплата за регистрацию, золотые горы."""
    low = norm(text)
    if SCAM_RE.search(low):
        return True
    if pay_max is not None and pay_period in ("день", "смена") and pay_max >= 15000:
        return True
    return pay_max is not None and pay_period == "месяц" and pay_max >= 500_000
