"""Обвязка веб-тестов откликов и переписки: два посетителя с отдельными cookie на одном приложении."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from fastapi.testclient import TestClient

from tests.accounts.harness import Env, login_as, post
from tests.messaging.conftest import add_vacancy

ACCOUNT = "/account"  # страница с формой выхода: с неё берётся CSRF-токен вошедшего пользователя


def second_visitor(env: Env) -> Env:
    return replace(env, client=TestClient(env.app, base_url="http://testserver"))


def submit(env: Env, path: str, data: dict[str, str] | None = None, **kw):
    """POST с актуальным CSRF-токеном вошедшего пользователя."""
    return post(env, path, data or {}, page=ACCOUNT, **kw)


def new_vacancy(env: Env, owner_id: int | None, **kw) -> int:
    conn = env.db()
    try:
        return add_vacancy(conn, owner_id, **kw)
    finally:
        conn.close()


def set_phone(env: Env, user_id: int, phone: str) -> None:
    conn = env.db()
    try:
        conn.execute("UPDATE users SET phone=? WHERE id=?", (phone, user_id))
        conn.commit()
    finally:
        conn.close()


@dataclass
class World:
    """Соискатель (seeker), работодатель (boss), вакансия и посторонние пользователи."""

    seeker: Env
    boss: Env
    seeker_id: int
    boss_id: int
    vacancy: int


def make_world(env: Env) -> World:
    boss = env
    seeker = second_visitor(env)
    boss_id = login_as(boss, "boss@example.test", role="employer", name="Олег Босс")
    seeker_id = login_as(seeker, "seeker@example.test", role="job_seeker", name="Мария Петрова")
    set_phone(env, seeker_id, "+79491112233")
    vacancy = new_vacancy(env, boss_id)
    return World(seeker, boss, seeker_id, boss_id, vacancy)


def stranger(env: Env, email: str = "stranger@example.test", role: str = "job_seeker") -> Env:
    visitor = second_visitor(env)
    login_as(visitor, email, role=role, name="Посторонний")
    return visitor


def apply_as_seeker(world: World, cover: str = "Здравствуйте, хочу работать") -> int:
    r = submit(world.seeker, f"/vacancy/{world.vacancy}/apply", {"cover": cover, "share_phone": "1"})
    assert r.status_code == 303, r.text
    return int(re.search(r"/responses/(\d+)", r.headers["location"]).group(1))


def thread_of(env: Env, application_id: int) -> int:
    rows = env.query("SELECT id FROM conversations WHERE application_id=?", application_id)
    return int(rows[0]["id"])
