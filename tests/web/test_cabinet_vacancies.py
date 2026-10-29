"""Кабинет работодателя: список вакансий, закрытие, поднятие, правка, удаление."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.accounts.harness import env_impl, login_as
from tests.cabinet.conftest import make_row
from tests.messaging.webkit import second_visitor, submit

env = pytest.fixture(env_impl)

FORM = {
    "profession": "Бариста", "category": "общепит", "city": "Донецк", "summary": "Готовить кофе и общаться с гостями.",
    "phone": "+7 949 000-00-00", "pay_min": "30000", "pay_max": "40000", "pay_period": "месяц",
    "employer": "Кофейня Зёрна", "schedule": "2/2",
}


def real_now() -> datetime:
    return datetime.now(UTC)


class Boss:
    def __init__(self, env):
        self.env = env
        env.clock.current = real_now()
        self.id = login_as(env, "boss@example.test", role="employer", name="Олег Босс")
        self.client = env.client

    def add(self, **kw) -> int:
        conn = self.env.db()
        try:
            kw.setdefault("seen", real_now() - timedelta(days=2))
            return make_row(conn, self.id, **kw)
        finally:
            conn.close()

    def row(self, vid):
        return self.env.query("SELECT * FROM vacancies WHERE id=?", vid)[0]

    def post(self, path, data=None, **kw):
        return submit(self.env, path, data or {}, **kw)


@pytest.fixture
def boss(env):
    return Boss(env)


# ------------------------------------------------------------------ доступ

@pytest.mark.parametrize("path", ["/cabinet/vacancies", "/cabinet/vacancies/1/edit", "/cabinet/vacancies/1/delete", "/cabinet/company"])
def test_guest_goes_to_login(env, path):
    r = env.client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("role", ["job_seeker", "admin"])
@pytest.mark.parametrize("path", ["/cabinet/vacancies", "/cabinet/vacancies/1/edit", "/cabinet/company"])
def test_other_roles_get_403(env, role, path):
    login_as(env, "x@example.test", role="job_seeker", name="Х")
    if role == "admin":
        c = env.db()
        c.execute("UPDATE users SET role='admin'")
        c.commit()
        c.close()
    assert env.client.get(path).status_code == 403


def test_posts_need_csrf(env, boss):
    vid = boss.add()
    for action in ("close", "raise", "delete"):
        r = env.client.post(f"/cabinet/vacancies/{vid}/{action}", data={}, follow_redirects=False)
        assert r.status_code == 403, action
    assert boss.row(vid)["status"] == "active"
    assert env.client.post("/cabinet/company", data={"name": "X"}, follow_redirects=False).status_code == 403


def test_removed_sections_are_gone(env, boss):
    assert env.client.get("/cabinet/resume").status_code == 404
    assert env.client.get("/cabinet/favorites").status_code == 404


# ------------------------------------------------------------------ список

def test_empty_state(env, boss):
    r = env.client.get("/cabinet/vacancies")
    assert r.status_code == 200
    assert "у вас пока нет вакансий" in r.text.lower()
    assert re.search(r'<a class="btn btn--primary[^"]*" href="/razmestit">Разместить', r.text)


def test_page_is_private_and_has_seo(env, boss):
    r = env.client.get("/cabinet/vacancies")
    assert 'name="robots" content="noindex' in r.text and "noindex" in r.headers["x-robots-tag"]
    assert r.headers["cache-control"] == "no-store"
    assert "<title>Мои вакансии" in r.text and 'rel="canonical"' in r.text and 'name="description"' in r.text
    assert r.text.count("<h1") == 1


def test_list_shows_status_dates_and_responses(env, boss):
    seeker = second_visitor(env)
    sid = login_as(seeker, "seeker@example.test", role="job_seeker", name="Мария")
    live = boss.add()
    boss.add(status="hidden", moderation="pending", profession="кассир")
    boss.add(status="hidden", moderation="rejected", note="Нет рабочего контакта", profession="повар")
    boss.add(status="closed", profession="грузчик")
    c = env.db()
    c.execute("INSERT INTO applications(vacancy_id, user_id) VALUES (?,?)", (live, sid))
    c.commit()
    c.close()
    html = env.client.get("/cabinet/vacancies").text
    for needle in ("Опубликована", "На проверке", "Отклонена", "Нет рабочего контакта", "Закрыта", "На сайте до",
                   "Кассир", "Повар", "Грузчик", f'href="/responses?vacancy={live}"', "Откликов: 1", "новый"):
        assert needle in html, needle
    assert f'href="/cabinet/vacancies/{live}/edit"' in html
    assert f'action="/cabinet/vacancies/{live}/close"' in html and f'action="/cabinet/vacancies/{live}/raise"' in html
    assert f'href="/cabinet/vacancies/{live}/delete"' in html


def test_only_own_vacancies_are_listed(env, boss):
    other_env = second_visitor(env)
    other = login_as(other_env, "other@example.test", role="employer", name="Чужой")
    c = env.db()
    make_row(c, other, profession="сварщик")
    c.close()
    boss.add(profession="бариста")
    html = env.client.get("/cabinet/vacancies").text
    assert "Бариста" in html and "Сварщик" not in html


def test_rate_limited_raise_shows_wait_instead_of_button(env, boss):
    vid = boss.add(seen=real_now() - timedelta(hours=3))
    html = env.client.get("/cabinet/vacancies").text
    assert f'action="/cabinet/vacancies/{vid}/raise"' not in html
    assert "Поднять можно через" in html


def test_filter_chips_and_no_results(env, boss):
    boss.add()
    boss.add(status="closed", profession="грузчик")
    html = env.client.get("/cabinet/vacancies").text
    assert 'href="/cabinet/vacancies?state=closed"' in html and 'href="/cabinet/vacancies?state=rejected"' not in html
    closed = env.client.get("/cabinet/vacancies?state=closed").text
    assert "Грузчик" in closed and "Бариста" not in closed
    none = env.client.get("/cabinet/vacancies?state=rejected").text
    assert "Нет вакансий с таким состоянием" in none and 'href="/cabinet/vacancies"' in none


def test_pagination_over_twenty(env, boss):
    for i in range(23):
        boss.add(profession=f"курьер {i}")
    page1 = env.client.get("/cabinet/vacancies").text
    assert page1.count('class="vrow vrow--') == 20 and "Страница 1 из 2" in page1 and 'rel="next"' in page1
    page2 = env.client.get("/cabinet/vacancies?page=2").text
    assert page2.count('class="vrow vrow--') == 3 and 'rel="prev"' in page2
    assert env.client.get("/cabinet/vacancies?page=zzz").status_code == 200


def test_flash_messages_are_known_codes_only(env, boss):
    ok = env.client.get("/cabinet/vacancies?done=posted").text
    assert "отправлена на проверку" in ok.lower()
    err = env.client.get("/cabinet/vacancies?err=too_soon").text
    assert "раз в сутки" in err
    evil = env.client.get("/cabinet/vacancies?done=<script>alert(1)</script>&err=<b>x</b>").text
    assert "<script>alert" not in evil and "<b>x</b>" not in evil


# ------------------------------------------------------------------ закрыть, поднять

def test_close(env, boss):
    vid = boss.add()
    r = boss.post(f"/cabinet/vacancies/{vid}/close")
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/vacancies?done=closed"
    assert boss.row(vid)["status"] == "closed"
    assert "Вакансия закрыта" in env.client.get(r.headers["location"]).text
    assert env.client.get(f"/vacancy/{vid}").status_code == 410


def test_close_foreign_is_404(env, boss):
    other_env = second_visitor(env)
    other = login_as(other_env, "other@example.test", role="employer", name="Чужой")
    c = env.db()
    vid = make_row(c, other)
    c.close()
    assert boss.post(f"/cabinet/vacancies/{vid}/close").status_code == 404
    assert boss.row(vid)["status"] == "active"
    assert boss.post("/cabinet/vacancies/99999/close").status_code == 404
    assert boss.post("/cabinet/vacancies/abc/close").status_code == 404


def test_raise_then_too_soon(env, boss):
    vid = boss.add(seen=real_now() - timedelta(days=5))
    r = boss.post(f"/cabinet/vacancies/{vid}/raise")
    assert r.headers["location"] == "/cabinet/vacancies?done=raised"
    stamp = boss.row(vid)["last_seen_at"]
    assert stamp > (real_now() - timedelta(minutes=5)).isoformat()
    again = boss.post(f"/cabinet/vacancies/{vid}/raise")
    assert again.status_code == 303 and again.headers["location"] == "/cabinet/vacancies?err=too_soon"
    assert boss.row(vid)["last_seen_at"] == stamp


def test_raise_pending_is_refused_with_message(env, boss):
    vid = boss.add(status="hidden", moderation="pending")
    r = boss.post(f"/cabinet/vacancies/{vid}/raise")
    assert r.headers["location"] == "/cabinet/vacancies?err=not_allowed"
    assert "нельзя" in env.client.get(r.headers["location"]).text


def test_reopen_closed(env, boss):
    vid = boss.add(status="closed")
    r = boss.post(f"/cabinet/vacancies/{vid}/raise")
    assert r.headers["location"] == "/cabinet/vacancies?done=reopened"
    assert boss.row(vid)["status"] == "active"


# ------------------------------------------------------------------ правка

def test_edit_form_is_prefilled(env, boss):
    vid = boss.add(schedule="2/2")
    r = env.client.get(f"/cabinet/vacancies/{vid}/edit")
    assert r.status_code == 200 and 'name="robots" content="noindex' in r.text
    for needle in ('value="Бариста"', 'value="2/2"', 'value="+79490000000"', 'value="30000"', f'action="/cabinet/vacancies/{vid}/edit"',
                   'name="csrf_token"'):
        assert needle in r.text, needle
    assert re.search(r'<option value="общепит" selected>', r.text)
    assert 'name="website"' not in r.text  # ловушка для ботов нужна гостевой форме


def test_edit_foreign_is_404(env, boss):
    other_env = second_visitor(env)
    other = login_as(other_env, "other@example.test", role="employer", name="Чужой")
    c = env.db()
    vid = make_row(c, other)
    c.close()
    assert env.client.get(f"/cabinet/vacancies/{vid}/edit").status_code == 404
    assert boss.post(f"/cabinet/vacancies/{vid}/edit", FORM).status_code == 404


def test_edit_saves_without_review(env, boss):
    vid = boss.add()
    r = boss.post(f"/cabinet/vacancies/{vid}/edit", {**FORM, "schedule": "5/2", "district": "Ворошиловский"})
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/vacancies?done=saved"
    row = boss.row(vid)
    assert row["schedule"] == "5/2" and row["status"] == "active" and row["moderation_status"] == "approved"


def test_edit_sensitive_goes_to_review(env, boss):
    vid = boss.add()
    r = boss.post(f"/cabinet/vacancies/{vid}/edit", {**FORM, "pay_max": "70000"})
    assert r.headers["location"] == "/cabinet/vacancies?done=review"
    row = boss.row(vid)
    assert row["moderation_status"] == "pending" and row["status"] == "hidden"
    text = env.client.get(r.headers["location"]).text
    assert "повторную проверку" in text and "На проверке" in text


def test_edit_rejected_resubmits(env, boss):
    vid = boss.add(status="hidden", moderation="rejected", note="Нет контакта")
    html = env.client.get(f"/cabinet/vacancies/{vid}/edit").text
    assert "Нет контакта" in html  # причина видна над формой
    r = boss.post(f"/cabinet/vacancies/{vid}/edit", FORM)
    assert r.headers["location"] == "/cabinet/vacancies?done=resubmitted"
    assert boss.row(vid)["moderation_status"] == "pending"


def test_edit_validation_errors_keep_input(env, boss):
    vid = boss.add()
    r = boss.post(f"/cabinet/vacancies/{vid}/edit", {**FORM, "profession": "аб", "phone": "123", "summary": "коротко", "schedule": "по вечерам"})
    assert r.status_code == 422
    for needle in ("error-summary", "Укажите профессию", "Телефон в формате", "хотя бы 20 символов", 'value="по вечерам"', 'value="аб"'):
        assert needle in r.text, needle
    assert boss.row(vid)["profession"] == "бариста" and 'noindex' in r.text


def test_edit_hidden_by_moderator_is_not_editable(env, boss):
    vid = boss.add(status="hidden", note="Скрыто администратором")
    assert env.client.get(f"/cabinet/vacancies/{vid}/edit").status_code == 403
    assert boss.post(f"/cabinet/vacancies/{vid}/edit", FORM).status_code == 403
    assert boss.row(vid)["status"] == "hidden"


# ------------------------------------------------------------------ удаление

def test_delete_needs_confirmation(env, boss):
    vid = boss.add()
    page = env.client.get(f"/cabinet/vacancies/{vid}/delete")
    assert page.status_code == 200 and f'action="/cabinet/vacancies/{vid}/delete"' in page.text and "Удалить навсегда" in page.text
    assert boss.row(vid)  # пока на месте
    r = boss.post(f"/cabinet/vacancies/{vid}/delete")
    assert r.headers["location"] == "/cabinet/vacancies?done=deleted"
    assert env.query("SELECT COUNT(*) AS n FROM vacancies WHERE id=?", vid)[0]["n"] == 0


def test_delete_page_warns_about_responses(env, boss):
    vid = boss.add()
    seeker = second_visitor(env)
    sid = login_as(seeker, "seeker@example.test", role="job_seeker", name="Мария")
    c = env.db()
    c.execute("INSERT INTO applications(vacancy_id, user_id) VALUES (?,?)", (vid, sid))
    c.commit()
    c.close()
    text = env.client.get(f"/cabinet/vacancies/{vid}/delete").text
    assert "1 отклик" in text and "закрыть" in text.lower()


def test_delete_foreign_is_404(env, boss):
    other_env = second_visitor(env)
    other = login_as(other_env, "other@example.test", role="employer", name="Чужой")
    c = env.db()
    vid = make_row(c, other)
    c.close()
    assert env.client.get(f"/cabinet/vacancies/{vid}/delete").status_code == 404
    assert boss.post(f"/cabinet/vacancies/{vid}/delete").status_code == 404
    assert env.query("SELECT COUNT(*) AS n FROM vacancies WHERE id=?", vid)[0]["n"] == 1
