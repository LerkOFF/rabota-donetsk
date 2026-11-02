"""Удаление аккаунта удаляет резюме и учёт просмотров; согласие хранится по каждому резюме."""
import pytest

from tests.accounts.harness import PASSWORD, env_impl
from tests.messaging.webkit import make_world, submit
from tests.web.resume_kit import create_id

env = pytest.fixture(env_impl)


def test_deleting_seeker_account_removes_resumes_views_and_links(env):
    w = make_world(env)
    first, second = create_id(w.seeker), create_id(w.seeker)
    submit(w.boss, f"/employers/resumes/{first}/contact")
    submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет", "resume_id": str(second)})
    assert env.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 2
    r = submit(w.seeker, "/account/delete", {"password": PASSWORD, "confirm": "1"})
    assert r.status_code == 303
    assert env.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0
    assert env.query("SELECT COUNT(*) AS n FROM resume_views")[0]["n"] == 0
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0
    assert w.boss.client.get(f"/employers/resumes/{first}").status_code == 404


def test_deleting_employer_account_keeps_resumes_but_drops_his_views(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    submit(w.boss, f"/employers/resumes/{rid}/contact")
    r = submit(w.boss, "/account/delete", {"password": PASSWORD, "confirm": "1"})
    assert r.status_code == 303
    assert env.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 1
    assert env.query("SELECT COUNT(*) AS n FROM resume_views")[0]["n"] == 0
