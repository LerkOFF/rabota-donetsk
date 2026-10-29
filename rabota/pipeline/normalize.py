"""Нормализация: сегмент поста -> Vacancy.

Структурные поля источника (trudvsem, hh, mczn, avito в `RawPost.extra`) точнее текста, поэтому
`apply_structured` накладывается поверх разобранного текста.
"""
from __future__ import annotations

import re
from typing import Any

from rabota.models import CATEGORIES, EMPLOYMENTS, WORK_FORMATS, RawPost, Vacancy
from rabota.pipeline.contacts import extract_contacts, extract_phones, extract_telegram
from rabota.pipeline.dictionaries import (
    ABBREVIATIONS,
    CONTEXT_CATEGORY_LEMMAS,
    DNR_CITIES,
    norm,
    profession_category,
)
from rabota.pipeline.fields import (
    detect_context_category,
    detect_suspicious,
    parse_age_min,
    parse_city,
    parse_district,
    parse_employer,
    parse_employment,
    parse_experience,
    parse_requirements,
    parse_schedule,
    parse_work_format,
)
from rabota.pipeline.homoglyphs import fix_homoglyphs
from rabota.pipeline.pay import Pay, monthly_estimate, parse_pay
from rabota.pipeline.split import Segment
from rabota.pipeline.titles import pick_profession

_META_LABEL = re.compile(
    r"^(?:город|формат|занятость|график(?:\s+работы)?|зарплата|заработная\s+плата|оплата|адрес|телефон|тел|связь|контакт\w*|"
    r"компания|локация|звоните|условия(?:\s+работы)?|требования|обязанности|мы\s+предлагаем|ставка|выплаты?|смена|"
    r"опыт(?:\s+работы)?|возраст|з/п|зп|место\s+работы|писать|пишите|пиши|для\s+связи|справки|подробности|информация)\b[^.!?\n]{0,40}:",
    re.IGNORECASE,
)
_PHONE_OR_HANDLE = re.compile(r"\+?\d[\d\s()\-]{8,}\d|@\w+|\bзвони\w*|\bобращаться\b|по\s+телефону|по\s+всем\s+вопросам|телеграм|telegram", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
SUMMARY_MAX = 220


def build_summary(text: str, profession: str = "", city: str | None = None) -> str:
    """1-2 чистых предложения: без эмодзи, капса, телефонов и служебных строк."""
    kept: list[str] = []
    for line in text.split("\n"):
        line = re.sub(r"^(?:-|\d{1,2}\.)\s*", "", line.strip())
        if not line:
            continue
        if _META_LABEL.match(line) or _PHONE_OR_HANDLE.search(line):
            continue
        if line.endswith(":") and len(line.split()) <= 6:
            continue
        if len(line.split()) < 3:
            continue
        kept.append(line.rstrip(";,"))
    sentences: list[str] = []
    for chunk in kept:
        for sent in _SENTENCE_SPLIT.split(chunk):
            sent = sent.strip()
            if len(sent.split()) >= 3:
                sentences.append(sent)
    summary = ""
    for sent in sentences[:2]:
        sent = sent if sent[-1] in ".!?" else sent + "."
        candidate = (summary + " " + sent).strip()
        if len(candidate) > SUMMARY_MAX and summary:
            break
        summary = candidate
    if not summary:
        base = profession.capitalize() if profession else "Вакансия"
        return f"{base}, {city}." if city else f"{base}."
    if len(summary) > SUMMARY_MAX:
        cut = summary[:SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(",;:- ")
        summary = cut + "."
    return summary[0].upper() + summary[1:]


def _pick(*values: Any) -> Any:
    for v in values:
        if v:
            return v
    return None


def infer_category(lemma: str, known: bool, full_text: str) -> str:
    category = profession_category(lemma) if known else "другое"
    if category == "другое" or lemma in CONTEXT_CATEGORY_LEMMAS:
        ctx = detect_context_category(full_text)
        if ctx:
            category = ctx
    return category if category in CATEGORIES else "другое"


def normalize_segment(
    seg: Segment,
    raw: RawPost,
    *,
    post_contacts: dict[str, Any] | None = None,
    post_text: str = "",
) -> Vacancy | None:
    """Собрать Vacancy из сегмента поста. None, если должность определить не удалось."""
    own = seg.text
    common = seg.common
    full = seg.full or own
    lemma, known, generic = seg.profession, True, False
    if lemma is None:
        choice = pick_profession(own)
        if choice is None:
            choice = pick_profession(full) if full != own else None
        if choice is None:
            lemma, known, generic = ("работник" if re.search(r"работник", norm(full)) else "сотрудник"), False, True
        else:
            lemma, known, generic = choice.lemma, choice.known, choice.generic

    schedule = _pick(parse_schedule(own), parse_schedule(common))
    pay = parse_pay(own, schedule)
    if not pay.has_amount and common:
        pay = parse_pay(common, schedule)
        if not pay.has_amount:
            pay = Pay(pay_daily=pay.pay_daily)
    city = _pick(parse_city(own), parse_city(common), parse_city(post_text or full))
    district = _pick(parse_district(own), parse_district(common), parse_district(post_text or full))
    category = infer_category(lemma, known, full)
    work_format = parse_work_format(full, category)
    employment = parse_employment(full, schedule)
    experience = _pick(parse_experience(own), parse_experience(common))
    age_min = _pick(parse_age_min(own), parse_age_min(common))
    requirements = _pick(parse_requirements(own), parse_requirements(common))
    employer = _pick(parse_employer(own), parse_employer(common))

    own_phones = extract_phones(own)
    contacts: dict[str, Any] = dict(post_contacts or extract_contacts(full, raw.source))
    if own_phones:
        contacts["phones"] = own_phones
        if contacts.get("max") and contacts["max"] not in own_phones:
            contacts.pop("max")
    own_tg = extract_telegram(own, raw.source)
    if own_tg:
        contacts["telegram"] = own_tg[0]

    title_raw = seg.title or next((ln.strip("- ").strip() for ln in own.split("\n") if ln.strip()), "")
    suspicious = detect_suspicious(post_text or full, pay.pay_max, pay.pay_period)
    if generic and not known and not pay.has_amount and lemma not in ("работник", "сотрудник"):
        return None
    if lemma in ("работник", "сотрудник") and not pay.has_amount and not re.search(r"\bна\s+\w{4,}", norm(own)):
        return None

    vac = Vacancy(
        profession=lemma,
        title_raw=title_raw[:200],
        category=category,
        work_format=work_format if work_format in WORK_FORMATS else None,
        employment=employment if employment in EMPLOYMENTS else None,
        schedule=schedule,
        pay_min=pay.pay_min,
        pay_max=pay.pay_max,
        pay_period=pay.pay_period if pay.has_amount else None,
        pay_daily=pay.pay_daily,
        pay_monthly_estimate=pay.pay_monthly_estimate,
        city=city,
        district=district,
        experience=experience,
        age_min=age_min,
        requirements=requirements,
        employer=employer,
        contacts=contacts,
        summary=build_summary(own if len(own.split()) > 3 else full, lemma, city),
        source_type=raw.source_type,
        source=raw.source,
        source_url=raw.url,
        external_id=raw.external_id,
        published_at=raw.published_at,
        kind="vacancy",
        suspicious=suspicious,
    )
    return vac


# ---------------------------------------------------------------- структурные поля источников

def tidy_employer(name: str) -> str:
    """Название работодателя без капса, с ёлочками вместо прямых кавычек."""
    name = re.sub(r"\s+", " ", fix_homoglyphs(name.strip()))
    out: list[str] = []
    opened = False
    for ch in name:
        if ch == '"':
            out.append("»" if opened else "«")
            opened = not opened
        else:
            out.append(ch)
    name = "".join(out).replace("« ", "«").replace(" »", "»").replace("»«", "» «")

    def fix(m: re.Match[str]) -> str:
        w = m.group(0)
        return w.capitalize() if w.isupper() and len(w) > 4 and w not in ABBREVIATIONS else w

    return re.sub(r"[A-Za-zА-Яа-яЁё]+", fix, name)[:120]


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", key.lower())


def find_extra(extra: Any, *names: str, depth: int = 3) -> Any:
    """Найти значение по имени ключа в extra (ищем и во вложенных словарях, регистр и дефисы не важны)."""
    wanted = {_norm_key(n) for n in names}
    queue: list[tuple[Any, int]] = [(extra, 0)]
    while queue:
        node, d = queue.pop(0)
        if isinstance(node, dict):
            for k, v in node.items():
                if _norm_key(str(k)) in wanted and v not in (None, "", [], {}):
                    return v
            if d < depth:
                queue.extend((v, d + 1) for v in node.values() if isinstance(v, dict | list))
        elif isinstance(node, list) and d < depth:
            queue.extend((v, d + 1) for v in node[:5] if isinstance(v, dict | list))
    return None


def _flatten_strings(node: Any, limit: int = 8) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_flatten_strings(v, limit))
    elif isinstance(node, list):
        for v in node:
            out.extend(_flatten_strings(v, limit))
    return out[:limit]


def _as_int(value: Any) -> int | None:
    if value in (None, "", False):
        return None
    try:
        n = int(float(str(value).replace(" ", "").replace(",", ".")))
    except ValueError:
        return None
    return n if n > 0 else None


def extra_text(extra: dict[str, Any]) -> str:
    """Текст вакансии из структурных полей, если у поста нет собственного текста."""
    parts: list[str] = []
    title = find_extra(extra, "job-name", "job_name", "title", "name", "profession", "vacancy_name", "position")
    if isinstance(title, str):
        parts.append(title)
    for key in ("duty", "responsibilities", "responsibility", "description", "requirement", "requirements", "snippet", "conditions"):
        v = find_extra(extra, key)
        if v:
            parts.extend(_flatten_strings(v, 4))
    return "\n".join(p for p in parts if p).strip()


def apply_structured(vac: Vacancy, extra: dict[str, Any]) -> Vacancy:
    """Наложить структурные поля источника на разобранную из текста вакансию."""
    if not extra:
        return vac
    smin = _as_int(find_extra(extra, "salary_min", "salary_from", "pay_min", "min_salary", "wage_min", "from"))
    smax = _as_int(find_extra(extra, "salary_max", "salary_to", "pay_max", "max_salary", "wage_max", "to"))
    currency = find_extra(extra, "currency", "currency_code", "currencycode")
    if isinstance(currency, str) and not re.sub(r"[^A-Za-zА-Яа-я₽]", "", currency).upper().startswith(("RUR", "RUB", "РУБ", "₽")):
        smin = smax = None
    salary_text = find_extra(extra, "salary")
    if isinstance(salary_text, str) and smin and smin == smax:
        low_salary = salary_text.strip().lower()
        if low_salary.startswith("от"):
            smax = None
        elif low_salary.startswith("до"):
            smin = None
    if smin or smax:
        vac.pay_min, vac.pay_max = smin or None, smax or None
        vac.pay_period = str(find_extra(extra, "pay_period", "salary_period") or "месяц")
        if vac.pay_period not in ("месяц", "смена", "день", "час", "задача"):
            vac.pay_period = "месяц"
        vac.pay_monthly_estimate = monthly_estimate(vac.pay_min, vac.pay_max, vac.pay_period, vac.schedule)
    company = find_extra(extra, "company", "employer", "company_name", "employer_name")
    if isinstance(company, dict):
        company = find_extra(company, "name", "company_name", "title", "shortname")
    if isinstance(company, str) and company.strip():
        vac.employer = tidy_employer(company)
    addr = find_extra(extra, "addresses", "address", "location", "area", "city", "town")
    if addr:
        addr_text = " ".join(_flatten_strings(addr))
        city = parse_city(addr_text)
        if city:
            vac.city = city
        district = parse_district(addr_text)
        if district:
            vac.district = district
    sched = find_extra(extra, "schedule", "work_schedule")
    emp = find_extra(extra, "employment", "employment_type")
    sched_text = " ".join(_flatten_strings(sched)) if sched else ""
    emp_text = " ".join(_flatten_strings(emp)) if emp else ""
    both = norm(sched_text + " " + emp_text)
    if both.strip():
        if re.search(r"удален|дистанц", both):
            vac.work_format = "удалённо"
        elif re.search(r"вахт", both):
            vac.work_format = "вахта"
        if re.search(r"частичн|подработ|времен|стажиров|проектн", both):
            vac.employment = "подработка"
        elif re.search(r"сменн|посменн", both):
            vac.employment = "сменная"
        elif re.search(r"полн", both):
            vac.employment = "постоянно"
        parsed = parse_schedule(sched_text)
        if parsed:
            vac.schedule = parsed
        elif re.search(r"гибк|свободн", both):
            vac.schedule = "свободный"
    exp = find_extra(extra, "experience")
    if exp is not None and not isinstance(exp, bool):
        exp_text = " ".join(_flatten_strings(exp)) if not isinstance(exp, int | float) else ""
        if isinstance(exp, int | float):
            vac.experience = "не нужен" if exp == 0 else (f"от {int(exp)} лет" if exp != 1 else "от 1 года")
        elif exp_text:
            parsed_exp = parse_experience("опыт " + exp_text) or parse_experience(exp_text)
            if re.search(r"нет\s+опыта|без\s+опыта", norm(exp_text)):
                parsed_exp = "не нужен"
            elif re.search(r"от\s+1\s+года\s+до|от\s+1\s+до", norm(exp_text)):
                parsed_exp = "от 1 года"
            vac.experience = parsed_exp or vac.experience
    phones = find_extra(extra, "phone", "phones", "contact_phone", "tel")
    if phones:
        found: list[str] = []
        for s in _flatten_strings(phones):
            found.extend(extract_phones(s))
        found = [p for p in dict.fromkeys(found)]
        if found:
            old = vac.contacts.get("phones", [])
            vac.contacts["phones"] = list(dict.fromkeys(found + old))
    email = find_extra(extra, "email", "contact_email")
    if isinstance(email, str) and "@" in email:
        vac.contacts.setdefault("email", email.strip().lower())
    return vac


def is_dnr(city: str | None) -> bool:
    """Город ДНР (для фильтра вакансий других регионов на стороне сайта)."""
    return city in DNR_CITIES if city else False
