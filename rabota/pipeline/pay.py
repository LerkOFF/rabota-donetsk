"""Оплата: число или вилка плюс период.

Примеры:
    "4000р. смена"            -> 4000, смена
    "от 48 500 - 56 000 ₽"    -> 48500-56000, месяц
    "1250 каждому за задачу"  -> 1250, задача
    "от 80 до 100 тыс."       -> 80000-100000, месяц
    "по договорённости"       -> оплата не указана
Месячный эквивалент (pay_monthly_estimate) считается отдельно и является оценкой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rabota.pipeline.dictionaries import norm

_NUM = r"\d{1,3}(?:[ .,'\u2019]\d{3})+(?!\d)|\d+(?:[.,]\d+)?"
_UNIT_TH = r"(?:тыс\w*\.?|к(?![а-яa-z0-9])|млн\w*\.?)"
_CUR = r"(?:₽|руб\w*\.?|рос\.?\s*руб\w*\.?|р(?![а-яa-z0-9])\.?)"

_CAND = re.compile(
    rf"(?P<pre>(?:от|до|около|порядка|примерно|~)\s*)?(?P<n1>{_NUM})\s*(?P<u1>{_UNIT_TH})?\s*(?P<c1>{_CUR})?"
    rf"(?:\s*(?:-|до|по)\s*(?P<n2>{_NUM})\s*(?P<u2>{_UNIT_TH})?\s*(?P<c2>{_CUR})?)?"
)

_PHONE_MASK = re.compile(r"(?:\+7|\b[78])[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}|\+7[\s\-(]*\d{3}[\s\-)]*\d[\d\s\-]{6,12}")
_TIME_MASK = re.compile(r"\b\d{1,2}[:.]\d{2}\b(?!\d)")
_SLASH_MASK = re.compile(r"\b\d{1,2}\s*/\s*\d{1,2}\b")
_DATE_MASK = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")

_KW_BEFORE = re.compile(
    r"(?:з\.?\s*/?\s*п\.?|зп|зарплат\w*|заработн\w+\s+плат\w*|оплат\w*|оклад|ставк\w*|доход\w*|выход|стоимость|цена|"
    r"смена|сумма|платим|платят|выплат\w*|получать|зарабатывать|начисл\w*)\W{0,14}$"
)
_KW_AFTER = re.compile(r"^\W{0,3}(?:оплата\b|выплата\b|выход\b|за\s+смену|смена\b|в\s+смену|в\s+день|за\s+день|в\s+месяц|в\s+час|за\s+час|после\s+смены)")

_BONUS_BEFORE = re.compile(
    r"(?:суточн\w*|премия|премии|бонус\w*|надбавк\w*|доплат\w*|подъ[её]мн\w*|штраф\w*|залог\w*|депозит\w*|"
    r"взнос\w*|единовремен\w*|проезд\w*|питани\w*|дополнительн\w+\s+выплат\w*|кредит\w*|ипотек\w*|долг\w*|"
    r"стаж\w*|вредност\w*|обучени\w*|аренд\w*|отпускн\w*)\W{0,14}$"
)
_BONUS_AFTER = re.compile(r"^\W{0,4}(?:за\s+тен|за\s+вредн|за\s+выслугу|за\s+стаж|за\s+каждого\s+приглашенн|за\s+приглашени)")
_NOT_MONEY_AFTER = re.compile(
    r"^\s*(?:%|раз\b|раза\b|человек|чел\b|лет\b|года\b|год\b|г\.|дн(?:я|ей|ям)\b|мин\b|минут|этаж|кг\b|позици|шт\b|"
    r"недел|мест\b|мест[аоу]\b|час(?:а|ов)?\b|смен[ыа]?\s+в|см\b|м2|м²|кв\b|тонн|км\b|литр)"
)

_PERIOD_AFTER: list[tuple[str, re.Pattern[str]]] = [
    ("смена", re.compile(r"^\s*/\s*\d+\s*час")),
    ("задача", re.compile(r"^[\s/]*(?:(?:каждому|на\s+каждого|на\s+человека)\s+)?(?:за\s+(?:задач\w*|работ[уы]|пролет|объект|единиц|тонн|метр|кв|м2|м²|каждый\s+м2|погонн|штуку|выезд|заказ|услуг)|м2|м²|кв\.?\s*м)")),
    ("месяц", re.compile(r"^[\s.,:/-]*(?:в\s+месяц|/\s*мес|за\s+месяц|мес\b|ежемесячно|на\s+руки\s+в\s+месяц)")),
    ("час", re.compile(r"^[\s.,:/-]*(?:в\s+час|за\s+час|/\s*час(?!ов)|/\s*ч\b|почасов)")),
    ("смена", re.compile(r"^[\s.,:/-]*(?:(?:за|в|на)\s+(?:12-?часов\w+\s+)?смен[уы]?|смен[аыу]?\b|за\s+выход|выход\b|после\s+смены|в\s+конце\s+смены|/\s*смен)")),
    ("день", re.compile(r"^[\s.,:/-]*(?:в\s+день|за\s+день|/\s*день|в\s+сутки|за\s+сутки|в\s+конце\s+(?:рабочего\s+)?дня|каждый\s+день|ежедневно|в\s+конце\s+дня)")),
]
_PERIOD_AFTER_LOOSE: list[tuple[str, re.Pattern[str]]] = [
    ("месяц", re.compile(r"\bв\s+месяц\b|/\s*мес\b|\bмесяц\w*")),
    ("смена", re.compile(r"за\s+смену|\bвыход\b|в\s+конце\s+смены|после\s+смены")),
    ("день", re.compile(r"в\s+конце\s+(?:рабочего\s+)?дня|ежедневн\w+\s+выплат|выплат\w*\s+ежедневн|каждый\s+день|в\s+день")),
]
_PERIOD_BEFORE: list[tuple[str, re.Pattern[str]]] = [
    ("смена", re.compile(r"(?:выход|смена|за\s+смену|ставка\s+за\s+смену)\W{0,12}$")),
    ("день", re.compile(r"(?:в\s+день|за\s+день|день)\W{0,8}$")),
    ("час", re.compile(r"(?:в\s+час|за\s+час|час)\W{0,8}$")),
]

_DAILY_RE = re.compile(
    r"(?:выплат\w*|оплат\w*|зарплат\w*|з/п|платим|заработн\w+\s+плат\w*)[^.\n]{0,25}ежедневн|"
    r"ежедневн\w*\s+(?:выплат|оплат)|выплат\w*[^.\n]{0,12}кажд\w+\s+(?:2|два)\s+дн|"
    r"кажд\w+\s+(?:2|два)\s+дн\w+|каждый\s+день|выплат\w*\s+сразу|оплат\w*\s+сразу|оплат\w*\s+в\s+конце\s+(?:рабочего\s+)?(?:дня|смены)|"
    r"в\s+конце\s+(?:рабочего\s+)?(?:дня|смены)|после\s+смены|по\s+окончани\w+\s+(?:смены|рабочего\s+дня)|"
    r"оплат\w*\s+по\s+окончани|ежедневная\s+оплата|ежедневная\s+выплата|выплаты\s+ежедневные|выплаты\s+каждый\s+день"
)

_NO_PAY_RE = re.compile(
    r"по\s+договор[её]нност|договорная|обсуждается|обсуждаем|зарплата\s+не\s+указана|зп\s+не\s+указана|"
    r"оплата\s+по\s+факту|по\s+результатам\s+собеседования|на\s+собеседовании"
)

_MONTHLY_DEFAULT_THRESHOLD = 10000


@dataclass
class Pay:
    pay_min: int | None = None
    pay_max: int | None = None
    pay_period: str | None = None
    pay_daily: bool = False
    pay_monthly_estimate: int | None = None

    @property
    def has_amount(self) -> bool:
        return self.pay_min is not None or self.pay_max is not None


def _to_number(raw: str) -> float:
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:[ .,'\u2019]\d{3})+", raw):
        return float(re.sub(r"[ .,'\u2019]", "", raw))
    return float(raw.replace(",", "."))


def _mask(text: str) -> str:
    out = text
    for rx in (_PHONE_MASK, _DATE_MASK, _TIME_MASK, _SLASH_MASK):
        out = rx.sub(lambda m: " " * len(m.group()), out)
    return out


def _multiplier(unit: str | None) -> int:
    if not unit:
        return 1
    return 1_000_000 if unit.startswith("млн") else 1000


@dataclass
class _Cand:
    start: int
    end: int
    lo: int | None
    hi: int | None
    score: int
    has_cur: bool


def _find_candidates(text: str) -> list[_Cand]:
    low = norm(text)
    low = re.sub("[\u2012\u2013\u2014\u2015\u2212]", "-", low)
    masked = _mask(low)
    cands: list[_Cand] = []
    for m in _CAND.finditer(masked):
        n1, n2 = m.group("n1"), m.group("n2")
        u1, u2 = m.group("u1"), m.group("u2")
        has_cur = bool(m.group("c1") or m.group("c2"))
        has_unit = bool(u1 or u2)
        start, end = m.start(), m.end()
        before = masked[max(0, start - 30):start]
        before_line = before.split("\n")[-1]
        after = masked[end:end + 40].split("\n")[0]
        # число сразу после плюса: бонус, не оплата
        if start > 0 and masked[start - 1] == "+" and not has_cur:
            continue
        if before_line.rstrip().endswith("+"):
            continue
        # числа с % и прочие не деньги
        tail = masked[(m.end("n2") if n2 else m.end("n1")):][:25]
        if not has_cur and not has_unit and _NOT_MONEY_AFTER.match(tail):
            continue
        kw_before = bool(_KW_BEFORE.search(before_line))
        kw_after = bool(_KW_AFTER.match(after))
        value1 = _to_number(n1) * _multiplier(u1 or (u2 if (n2 and _to_number(n1) < 1000) else None))
        value2 = _to_number(n2) * _multiplier(u2) if n2 else None
        if not (has_cur or has_unit or kw_before or kw_after):
            continue
        if not has_cur and not has_unit and value1 < 500:
            continue
        if _BONUS_BEFORE.search(before_line) or _BONUS_AFTER.match(after):
            continue
        if value1 > 20_000_000:
            continue
        if n2 and value2 is not None and value2 < value1:
            # "с 8 до 6": не вилка; "5-4": ошибка
            if not has_cur:
                continue
            value1, value2 = value2, value1
        pre = (m.group("pre") or "").strip()
        lo: int | None
        hi: int | None
        if value2 is not None:
            lo, hi = int(value1), int(value2)
        elif pre == "до":
            lo, hi = None, int(value1)
        elif pre == "от":
            lo, hi = int(value1), None
        else:
            lo, hi = int(value1), int(value1)
        score = (2 if value2 is not None else 0) + (3 if kw_before else 0) + (1 if has_cur else 0) + (1 if has_unit else 0)
        cands.append(_Cand(start, end, lo, hi, score, has_cur))
    return cands


def _detect_period(low: str, cand: _Cand, amount: int) -> str | None:
    after = low[cand.end:cand.end + 45].split("\n")[0]
    for period, rx in _PERIOD_AFTER:
        if rx.match(after):
            return period
    before = low[max(0, cand.start - 30):cand.start].split("\n")[-1]
    for period, rx in _PERIOD_BEFORE:
        if rx.search(before):
            return period
    line_start = low.rfind("\n", 0, cand.start) + 1
    line_end = low.find("\n", cand.end)
    line = low[line_start: line_end if line_end != -1 else len(low)]
    for period, rx in _PERIOD_AFTER_LOOSE:
        if rx.search(line):
            return period
    if amount >= _MONTHLY_DEFAULT_THRESHOLD:
        return "месяц"
    return "смена"


def _shifts_per_month(schedule: str | None) -> int:
    if schedule:
        m = re.match(r"(\d{1,2})/(\d{1,2})", schedule)
        if m:
            on, off = int(m.group(1)), int(m.group(2))
            if on + off > 0 and on + off <= 14:
                return max(1, round(30 * on / (on + off)))
            if on + off > 14:
                return 15
        if "сутки" in schedule:
            return 10
    return 0


def monthly_estimate(pay_min: int | None, pay_max: int | None, period: str | None, schedule: str | None = None) -> int | None:
    """Оценка заработка в месяц. Для вилки среднее, для "до N" берём N, для "от N" берём N."""
    if period is None or (pay_min is None and pay_max is None):
        return None
    if pay_min is not None and pay_max is not None:
        base = (pay_min + pay_max) / 2
    else:
        base = pay_min if pay_min is not None else pay_max
    assert base is not None
    if period == "месяц":
        est = base
    elif period == "смена":
        est = base * (_shifts_per_month(schedule) or 15)
    elif period == "день":
        est = base * (_shifts_per_month(schedule) or 22)
    elif period == "час":
        est = base * 8 * 22
    else:
        return None
    return int(round(est / 100.0) * 100)


def parse_pay(text: str, schedule: str | None = None) -> Pay:
    """Достать оплату из текста вакансии (или её части)."""
    if not text:
        return Pay()
    low = norm(text)
    low = re.sub("[\u2012\u2013\u2014\u2015\u2212]", "-", low)
    daily = bool(_DAILY_RE.search(low))
    cands = _find_candidates(text)
    if not cands:
        return Pay(pay_daily=daily)
    best = max(cands, key=lambda c: (c.score, -c.start))
    amount = best.hi if best.hi is not None else (best.lo or 0)
    period = _detect_period(low, best, best.lo if best.lo is not None else amount)
    pay = Pay(best.lo, best.hi, period, daily)
    pay.pay_monthly_estimate = monthly_estimate(best.lo, best.hi, period, schedule)
    return pay


def mentions_negotiable_pay(text: str) -> bool:
    """В тексте сказано "по договорённости" и подобное, числа нет."""
    return bool(_NO_PAY_RE.search(norm(text)))
