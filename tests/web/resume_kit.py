"""Обвязка веб-тестов резюме: анкета, создание через форму, работодатели и соискатели с отдельными cookie."""
from __future__ import annotations

from tests.accounts.harness import Env, login_as
from tests.messaging.webkit import second_visitor, submit

FORM = {
    "profession": "Бариста", "city": "Донецк", "district": "Ворошиловский", "pay_min": "30000", "pay_period": "месяц",
    "work_formats": ["точка"], "employments": ["постоянно"], "schedule": "2/2", "experience_years": "3",
    "place_1": "Кофейня Зерно", "role_1": "Бариста", "period_1": "2022-2024", "skills": "кофе, касса, сервис",
    "about": "Работаю быстро и аккуратно, люблю кофе и общение с гостями, быстро учусь и не боюсь сложных смен.",
    "contact_phone": "+7 949 111-22-33", "contact_telegram": "@maria_p", "visibility": "public", "consent": "1",
}


def form(**over) -> dict:
    data = {**FORM, **over}
    return {k: v for k, v in data.items() if v is not None}


def create(env: Env, **over):
    """POST новой анкеты. Возвращает ответ."""
    return submit(env, "/cabinet/resumes/new", form(**over))


def create_id(env: Env, **over) -> int:
    r = create(env, **over)
    assert r.status_code == 303, r.text
    rows = env.query("SELECT id FROM resumes ORDER BY id DESC LIMIT 1")
    return int(rows[0]["id"])


def employer_visitor(env: Env, email: str = "boss@example.test", name: str = "Олег Босс") -> tuple[Env, int]:
    visitor = second_visitor(env)
    uid = login_as(visitor, email, role="employer", name=name)
    return visitor, uid
