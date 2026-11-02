"""Фикстуры тестов резюме: чистая база, пользователи и вакансии напрямую в SQL, анкета для проверки."""
from __future__ import annotations

from pathlib import Path

import pytest

from rabota.accounts import service as accounts
from rabota.resumes import rules, service
from tests.messaging.conftest import NOW, add_user, add_vacancy  # noqa: F401

FORM = {
    "profession": "Бариста", "city": "Донецк", "district": "Ворошиловский", "pay_min": "30000", "pay_period": "месяц",
    "work_formats": ["точка"], "employments": ["постоянно"], "schedule": "2/2", "experience_years": "3",
    "place_1": "Кофейня Зерно", "role_1": "Бариста", "period_1": "2022-2024", "skills": "кофе, касса, сервис",
    "about": "Работаю быстро и аккуратно, люблю кофе и общение с гостями, быстро учусь и не боюсь сложных смен.", "contact_phone": "+7 949 111-22-33", "contact_telegram": "@maria_p",
    "visibility": "public", "consent": "1",
}


class Form(dict):
    """Форма для rules.validate: словарь со списками для флажков."""

    def getlist(self, name: str) -> list[str]:
        value = self.get(name, [])
        return list(value) if isinstance(value, list) else ([value] if value else [])


def make_form(**over) -> Form:
    data = {**FORM, **over}
    return Form({k: v for k, v in data.items() if v is not None})


def clean_of(**over) -> dict:
    clean, errors = rules.validate(make_form(**over))
    assert not errors, errors
    return clean


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "resumes.db"
    accounts.ensure_schema(path)
    c = accounts.open_db(path)
    yield c
    c.close()


@pytest.fixture
def seeker(conn):
    return add_user(conn, "seeker@example.test", "job_seeker", "Мария Петрова", "+79491112233")


@pytest.fixture
def employer(conn):
    return add_user(conn, "boss@example.test", "employer", "Олег Босс")


@pytest.fixture
def resume_id(conn, seeker) -> int:
    return service.create(conn, seeker["id"], clean_of(), NOW)
