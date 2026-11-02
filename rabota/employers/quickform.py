"""Короткая форма размещения: проверка поверх rabota.web.postform.

Обязательные поля короткой формы: профессия, телефон или Telegram, описание. Сфера определяется по названию
профессии, если не выбрана, период оплаты по умолчанию "месяц". Гость дополнительно указывает почту для кода
и подтверждает согласие на обработку данных.
"""
from __future__ import annotations

from typing import Any

from rabota.accounts.service import normalize_email, valid_email
from rabota.models import CATEGORIES, Vacancy
from rabota.web.postform import validate_post

DEFAULT_PERIOD = "месяц"
DEFAULT_CATEGORY = "другое"


def guess_category(profession: str, summary: str = "") -> str:
    """Сфера по названию профессии и описанию тем же словарём, что у сборщиков. Не определилась: "другое"."""
    try:
        from rabota.pipeline.normalize import infer_category
        from rabota.pipeline.titles import pick_profession

        choice = pick_profession(profession)
        if choice is None:
            return infer_category("сотрудник", False, f"{profession} {summary}")
        return infer_category(choice.lemma, choice.known, f"{profession} {summary}")
    except Exception:  # noqa: BLE001 - словарь не должен ронять размещение
        return DEFAULT_CATEGORY


def prepare(form: dict[str, str]) -> dict[str, str]:
    """Дополняет форму умолчаниями: сфера по профессии, период оплаты "месяц", город Донецк."""
    data = dict(form)
    if (data.get("category") or "").strip() not in CATEGORIES:
        data["category"] = guess_category(" ".join((data.get("profession") or "").split()), data.get("summary") or "")
    has_pay = any("".join(ch for ch in (data.get(k) or "") if ch.isdigit()) for k in ("pay_min", "pay_max"))
    if has_pay and not (data.get("pay_period") or "").strip():
        data["pay_period"] = DEFAULT_PERIOD
    return data


def validate_employer(form: dict[str, str]) -> tuple[Vacancy | None, dict[str, str], dict[str, str]]:
    """Вошедший работодатель: та же проверка, что у кабинета, плюс умолчания короткой формы."""
    return validate_post(prepare(form))


def validate_guest(form: dict[str, str]) -> tuple[Vacancy | None, dict[str, str], dict[str, str], str]:
    """Гость: вакансия, почта для кода и согласие. Возвращает (вакансия, очищенные значения, ошибки, почта)."""
    vacancy, cleaned, errors = validate_post(prepare(form))
    email = normalize_email(form.get("account_email", ""))[:254]
    cleaned["account_email"] = email
    cleaned["consent"] = "1" if form.get("consent") == "1" else ""
    if not valid_email(email):
        errors["account_email"] = "Проверьте почту: на неё придёт код, он должен выглядеть как name@example.ru."
    if form.get("consent") != "1":
        errors["consent"] = "Без согласия на обработку персональных данных разместить вакансию нельзя."
    cleaned["category"] = cleaned.get("category", "") if cleaned.get("category") in CATEGORIES else ""
    return (None if errors else vacancy), cleaned, errors, email


def draft_form(cleaned: dict[str, Any]) -> dict[str, str]:
    """Что сохранить в черновике: очищенные значения вакансии без служебных полей гостя."""
    skip = {"account_email", "consent"}
    return {k: str(v) for k, v in cleaned.items() if k not in skip}
