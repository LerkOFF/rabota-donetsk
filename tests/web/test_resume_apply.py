"""Отклик с резюме и ход отклика: резюме подставляется, работодатель его видит, статусы с датами заметны."""
import re

import pytest

from tests.accounts.harness import env_impl
from tests.messaging.webkit import apply_as_seeker, make_world, stranger, submit
from tests.web.resume_kit import create_id

env = pytest.fixture(env_impl)


def apply(w, rid=None, **extra):
    data = {"cover": "Здравствуйте, хочу работать", "share_phone": "1", **extra}
    if rid is not None:
        data["resume_id"] = str(rid)
    return submit(w.seeker, f"/vacancy/{w.vacancy}/apply", data)


def aid_of(r) -> int:
    return int(re.search(r"/responses/(\d+)", r.headers["location"]).group(1))


def test_vacancy_page_offers_the_resume(env):
    w = make_world(env)
    page = w.seeker.client.get(f"/vacancy/{w.vacancy}").text
    assert "Составьте за пару минут" in page and 'name="resume_id"' not in page
    rid = create_id(w.seeker)
    page = w.seeker.client.get(f"/vacancy/{w.vacancy}").text
    assert 'name="resume_id"' in page and f'value="{rid}" selected' in page and "Откликнуться без резюме" in page
    assert "Бариста, от 30" in page


def test_hidden_resume_is_not_offered(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    submit(w.seeker, f"/cabinet/resumes/{rid}/hide")
    assert 'name="resume_id"' not in w.seeker.client.get(f"/vacancy/{w.vacancy}").text


def test_apply_page_has_resume_select_preselected(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    page = w.seeker.client.get(f"/vacancy/{w.vacancy}/apply").text
    assert 'id="ap-resume"' in page and f'value="{rid}" selected' in page and "(100%)" in page


def test_apply_with_resume_links_it_to_the_response(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    r = apply(w, rid)
    assert r.status_code == 303
    assert env.query("SELECT resume_id FROM applications")[0]["resume_id"] == rid
    assert env.query("SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 1


def test_apply_without_resume_still_works(env):
    w = make_world(env)
    create_id(w.seeker)
    assert apply(w, None, resume_id="").status_code == 303
    assert env.query("SELECT resume_id FROM applications")[0]["resume_id"] is None


def test_foreign_or_hidden_resume_cannot_be_attached(env):
    w = make_world(env)
    other = stranger(env)
    foreign = create_id(other)
    r = apply(w, foreign)
    assert r.status_code == 422 and "нельзя приложить" in r.text
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0
    mine = create_id(w.seeker)
    submit(w.seeker, f"/cabinet/resumes/{mine}/hide")
    assert apply(w, mine).status_code == 422
    assert apply(w, 99999).status_code == 422
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_admin_hidden_resume_cannot_be_attached(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    conn = env.db()
    conn.execute("UPDATE resumes SET admin_hidden=1")
    conn.commit()
    conn.close()
    assert apply(w, rid).status_code == 422


def test_failed_apply_keeps_resume_choice(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    r = apply(w, rid, cover="я" * 2100)
    assert r.status_code == 422 and f'value="{rid}" selected' in r.text


def test_employer_sees_resume_on_response_page(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    aid = aid_of(apply(w, rid))
    page = w.boss.client.get(f"/responses/{aid}").text
    assert "Бариста" in page and f'href="/employers/resumes/{rid}"' in page and "Открыть резюме" in page
    card = w.boss.client.get(f"/employers/resumes/{rid}")
    assert card.status_code == 200 and "+7 949" not in card.text


def test_employer_sees_no_resume_when_not_attached_or_hidden_later(env):
    w = make_world(env)
    aid = aid_of(apply(w))
    assert "не приложил резюме" in w.boss.client.get(f"/responses/{aid}").text
    other = stranger(env)  # один соискатель - один отклик, поэтому скрытие проверяем на втором
    rid2 = create_id(other)
    r = submit(other, f"/vacancy/{w.vacancy}/apply", {"cover": "Я тоже", "resume_id": str(rid2)})
    aid2 = aid_of(r)
    assert f"/employers/resumes/{rid2}" in w.boss.client.get(f"/responses/{aid2}").text
    submit(other, f"/cabinet/resumes/{rid2}/hide")
    page = w.boss.client.get(f"/responses/{aid2}").text
    assert f"/employers/resumes/{rid2}" not in page and "скрыл его" in page
    assert w.boss.client.get(f"/employers/resumes/{rid2}").status_code == 404


def test_seeker_sees_attached_resume_on_response_page(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    aid = aid_of(apply(w, rid))
    page = w.seeker.client.get(f"/responses/{aid}").text
    assert "Приложенное резюме" in page and f"/cabinet/resumes/{rid}/edit" in page


def test_deleted_resume_leaves_response_intact(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    aid = aid_of(apply(w, rid))
    submit(w.seeker, f"/cabinet/resumes/{rid}/delete")
    assert w.seeker.client.get(f"/responses/{aid}").status_code == 200
    assert "не приложил резюме" in w.boss.client.get(f"/responses/{aid}").text
    assert env.query("SELECT resume_id FROM applications")[0]["resume_id"] is None


# ------------------------------------------------------------------ ход отклика

def test_status_steps_are_visible_with_dates(env):
    w = make_world(env)
    aid = apply_as_seeker(w)
    lst = w.seeker.client.get("/responses").text
    assert 'class="steps"' in lst and "Отправлен" in lst and "Просмотрен" in lst and "ожидается" in lst
    assert "Сегодня" in lst or "29 октября" in lst
    w.boss.client.get(f"/responses/{aid}")  # работодатель открыл отклик
    env.clock.advance(hours=2)
    page = w.seeker.client.get("/responses").text
    assert "steps__item--viewed" in page and "steps__item--done" in page
    row = env.query("SELECT status, viewed_at FROM applications")[0]
    assert row["status"] == "viewed" and row["viewed_at"]


def test_invited_and_rejected_steps_show_decision_and_date(env):
    w = make_world(env)
    aid = apply_as_seeker(w)
    submit(w.boss, f"/responses/{aid}/status", {"status": "invited"})
    page = w.seeker.client.get(f"/responses/{aid}").text
    assert "Приглашён" in page and "steps__item--invited" in page and "Приглашение" in page
    assert w.seeker.query("SELECT viewed_at FROM applications")[0]["viewed_at"]
    submit(w.boss, f"/responses/{aid}/status", {"status": "rejected"})
    page = w.seeker.client.get(f"/responses/{aid}").text
    assert "steps__item--rejected" in page and "Отказ" in page and re.search(r"Отказ</strong><span class=\"steps__when\">[^<]+\d", page)


def test_withdrawn_steps(env):
    w = make_world(env)
    aid = apply_as_seeker(w)
    submit(w.seeker, f"/responses/{aid}/withdraw")
    page = w.seeker.client.get(f"/responses/{aid}").text
    assert "steps__item--withdrawn" in page and "Отозван" in page


def test_employer_list_has_steps_too(env):
    w = make_world(env)
    apply_as_seeker(w)
    page = w.boss.client.get("/responses").text
    assert 'class="steps"' in page and "Получен" in page
