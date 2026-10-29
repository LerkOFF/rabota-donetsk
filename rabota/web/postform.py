"""Проверка формы вакансии: общая для размещения (/razmestit) и правки в кабинете работодателя."""
from __future__ import annotations

from typing import Any

from rabota.models import CATEGORIES, EMPLOYMENTS, PAY_PERIODS, WORK_FORMATS, Vacancy
from rabota.web import format as fmt


def _clean(form: dict[str, str], name: str, limit: int) -> str:
    return " ".join((form.get(name) or "").split())[:limit]


def _int(form: dict[str, str], name: str, errors: dict[str, str], lo: int, hi: int, label: str) -> int | None:
    raw = "".join(ch for ch in (form.get(name) or "") if ch.isdigit())
    if not raw:
        return None
    n = int(raw[:9])
    if not lo <= n <= hi:
        errors[name] = f"{label}: число от {lo} до {hi}."
        return None
    return n


def validate_post(form: dict[str, str]) -> tuple[Vacancy | None, dict[str, str], dict[str, str]]:
    """Проверка формы размещения. Возвращает вакансию, очищенные значения и ошибки по полям."""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {
        "profession": _clean(form, "profession", 80),
        "category": _clean(form, "category", 30),
        "work_format": _clean(form, "work_format", 30),
        "employment": _clean(form, "employment", 30),
        "schedule": _clean(form, "schedule", 60),
        "pay_min": _clean(form, "pay_min", 9),
        "pay_max": _clean(form, "pay_max", 9),
        "pay_period": _clean(form, "pay_period", 20),
        "pay_daily": "1" if form.get("pay_daily") else "",
        "no_experience": "1" if form.get("no_experience") else "",
        "city": _clean(form, "city", 60) or "Донецк",
        "district": _clean(form, "district", 60),
        "age_min": _clean(form, "age_min", 3),
        "requirements": _clean(form, "requirements", 400),
        "employer": _clean(form, "employer", 120),
        "phone": _clean(form, "phone", 30),
        "telegram": _clean(form, "telegram", 64),
        "email": _clean(form, "email", 120),
        "summary": " ".join((form.get("summary") or "").split())[:1000],
    }
    if len(cleaned["profession"]) < 3:
        errors["profession"] = "Укажите профессию: хотя бы три буквы."
    if cleaned["category"] not in CATEGORIES:
        errors["category"] = "Выберите сферу из списка."
    if cleaned["work_format"] and cleaned["work_format"] not in WORK_FORMATS:
        errors["work_format"] = "Выберите формат из списка."
    if cleaned["employment"] and cleaned["employment"] not in EMPLOYMENTS:
        errors["employment"] = "Выберите тип занятости из списка."
    if cleaned["pay_period"] and cleaned["pay_period"] not in PAY_PERIODS:
        errors["pay_period"] = "Выберите период оплаты из списка."
    pay_min = _int(form, "pay_min", errors, 1, 5_000_000, "Оплата")
    pay_max = _int(form, "pay_max", errors, 1, 5_000_000, "Оплата")
    if (pay_min or pay_max) and not cleaned["pay_period"] and "pay_period" not in errors:
        errors["pay_period"] = "Укажите, за что платите: за месяц, смену, день или час."
    if pay_min and pay_max and pay_min > pay_max:
        errors["pay_max"] = "Максимум не может быть меньше минимума."
    age_min = _int(form, "age_min", errors, 14, 80, "Возраст")

    phone = ""
    if cleaned["phone"]:
        norm = fmt.normalize_phone(cleaned["phone"])
        if norm is None:
            errors["phone"] = "Телефон в формате +7 949 123-45-67."
        else:
            phone = norm
            cleaned["phone"] = norm
    telegram = cleaned["telegram"]
    if telegram and not _valid_telegram(telegram):
        errors["telegram"] = "Ник Telegram вида @name: латиница, цифры, подчёркивание."
    email = cleaned["email"]
    if email and ("@" not in email or " " in email or "." not in email.rsplit("@", 1)[-1]):
        errors["email"] = "Проверьте адрес почты."
    if not (phone or telegram or email) and "phone" not in errors and "telegram" not in errors:
        errors["phone"] = "Оставьте телефон или ник Telegram, иначе соискатель не сможет связаться."
    if len(cleaned["summary"]) < 20:
        errors["summary"] = "Опишите работу в паре предложений: хотя бы 20 символов."
    if errors:
        return None, cleaned, errors

    contacts: dict[str, Any] = {}
    if phone:
        contacts["phones"] = [phone]
    if telegram:
        contacts["telegram"] = telegram if telegram.startswith("@") else "@" + telegram
    if email:
        contacts["email"] = email
    vacancy = Vacancy(
        profession=cleaned["profession"].lower(),
        title_raw=cleaned["profession"],
        category=cleaned["category"],
        work_format=cleaned["work_format"] or None,
        employment=cleaned["employment"] or None,
        schedule=cleaned["schedule"] or None,
        pay_min=pay_min,
        pay_max=pay_max,
        pay_period=cleaned["pay_period"] or None,
        pay_daily=bool(cleaned["pay_daily"]),
        city=cleaned["city"],
        district=cleaned["district"] or None,
        experience="не нужен" if cleaned["no_experience"] else None,
        age_min=age_min,
        requirements=cleaned["requirements"] or None,
        employer=cleaned["employer"] or None,
        contacts=contacts,
        summary=cleaned["summary"],
    )
    return vacancy, cleaned, errors


def _valid_telegram(value: str) -> bool:
    v = value.lstrip("@")
    return 4 <= len(v) <= 32 and all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in v)


def form_options() -> dict[str, Any]:
    """Списки для выпадающих полей формы вакансии (подпись по значению)."""
    return {
        "categories": [(c, fmt.CATEGORY_LABELS[c]) for c in CATEGORIES],
        "work_formats": [(w, fmt.WORK_FORMAT_LABELS[w]) for w in WORK_FORMATS],
        "employments": [(e, fmt.EMPLOYMENT_LABELS[e]) for e in EMPLOYMENTS],
        "periods": [(p, fmt.PERIOD_LABELS[p]) for p in PAY_PERIODS],
    }
