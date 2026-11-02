"""Кабинет соискателя: /cabinet/resumes, форма, правка, скрытие, обновление, удаление, лимит, права."""
import re

import pytest

from rabota.resumes import rules
from tests.accounts.harness import build_env, env_impl, login_as, make_user
from tests.messaging.webkit import make_world, second_visitor, stranger, submit
from tests.web.resume_kit import create, create_id, form

env = pytest.fixture(env_impl)


@pytest.fixture
def seeker(env):
    login_as(env, "anna@example.test", role="job_seeker", name="Анна Иванова")
    return env


def row(env, rid=None):
    rows = env.query("SELECT * FROM resumes" + (" WHERE id = ?" if rid else " ORDER BY id DESC"), *([rid] if rid else []))
    return rows[0]


# ------------------------------------------------------------------ доступ

def test_guest_goes_to_login(env):
    for path in ("/cabinet/resumes", "/cabinet/resumes/new", "/cabinet/resumes/1/edit", "/cabinet/resumes/1/delete"):
        r = env.client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
        assert "noindex" in r.headers["x-robots-tag"]


def test_employer_gets_403_on_seeker_pages(env):
    login_as(env, "boss@example.test", role="employer")
    for path in ("/cabinet/resumes", "/cabinet/resumes/new", "/cabinet/resumes/1/edit"):
        assert env.client.get(path).status_code == 403, path


def test_unverified_user_is_not_let_in(env):
    make_user(env, "new@example.test", verified=False)
    r = env.client.post("/login", data={"email": "new@example.test", "password": "tulip-garden-77"})
    assert env.client.get("/cabinet/resumes", follow_redirects=False).status_code in (303, 403)
    assert r.status_code in (200, 303, 400, 403, 422)


# ------------------------------------------------------------------ страницы и SEO

def test_empty_list_page(seeker):
    r = seeker.client.get("/cabinet/resumes")
    assert r.status_code == 200
    assert "Резюме пока нет" in r.text and 'href="/cabinet/resumes/new"' in r.text
    assert re.search(r'<meta name="robots" content="noindex', r.text) and r.headers["cache-control"] == "no-store"
    assert "noindex" in r.headers["x-robots-tag"] and r.text.count("<h1") == 1
    assert '<link rel="canonical"' in r.text and "<title>" in r.text


def test_form_page_has_all_fields_and_consent(seeker):
    r = seeker.client.get("/cabinet/resumes/new")
    assert r.status_code == 200
    for needle in ('name="profession"', 'name="pay_min"', 'name="pay_period"', 'name="district"', 'name="relocate"',
                   'name="remote"', 'name="work_formats"', 'name="employments"', 'name="schedule"', 'name="experience_years"',
                   'name="place_1"', 'name="skills"', 'name="about"', 'name="contact_phone"', 'name="contact_telegram"',
                   'name="visibility"', 'name="consent"', 'name="csrf_token"', 'name="website"', 'list="rs-professions"',
                   'href="/legal/resume"', "Всем работодателям", "Только по отклику", "Скрыто", 'maxlength="500"'):
        assert needle in r.text, needle
    assert "бариста" in r.text.lower() and 'value="Ворошиловский"' in r.text
    assert 'name="consent" value="1" checked' not in r.text  # согласие не отмечено заранее


def test_account_phone_is_prefilled(env):
    uid = login_as(env, "p@example.test", name="Пётр")
    conn = env.db()
    conn.execute("UPDATE users SET phone='+79490001122' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    assert "+79490001122" in env.client.get("/cabinet/resumes/new").text


# ------------------------------------------------------------------ создание

def test_create_resume_in_one_form(seeker):
    r = create(seeker)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/resumes?done=created"
    saved = row(seeker)
    assert saved["title"] == "Бариста" and saved["pay_min"] == 30000 and saved["visibility"] == "public"
    assert saved["contact_phone"] == "+79491112233" and saved["consent_at"] and saved["consent_version"].startswith("resume ")
    page = seeker.client.get(r.headers["location"])
    assert "Резюме сохранено" in page.text and "Бариста" in page.text and "100%" in page.text
    assert "Видно работодателям" in page.text


def test_minimal_resume_only_needs_profession(seeker):
    r = submit(seeker, "/cabinet/resumes/new", {"profession": "Курьер", "visibility": "hidden"})
    assert r.status_code == 303
    page = seeker.client.get("/cabinet/resumes").text
    assert "Курьер" in page and "Что добавить" in page and "Скрыто" in page


def test_validation_errors_keep_input_and_are_announced(seeker):
    r = create(seeker, profession="", pay_min="30000", pay_period="", about="я" * 501, skills="a, b, " + "x" * 40, consent=None)
    assert r.status_code == 422
    for needle in ('role="alert"', "Нужно поправить", 'id="err-profession"', 'id="err-about"', 'id="err-skills"', 'id="err-pay_period"',
                   'id="err-consent"', 'aria-invalid="true"'):
        assert needle in r.text, needle
    assert 'value="30000"' in r.text and "кофе" in r.text.lower()  # введённое не потеряно
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0


def test_consent_is_required_for_open_resume(seeker):
    for vis in ("public", "open"):
        r = create(seeker, visibility=vis, consent=None)
        assert r.status_code == 422 and "согласие" in r.text.lower()
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0
    assert create(seeker, visibility="hidden", consent=None).status_code == 303


def test_consent_time_and_version_are_stored_per_resume(seeker):
    create(seeker)
    r = row(seeker)
    assert r["consent_at"] and r["consent_version"] == rules.consent_version() and r["consent_withdrawn_at"] is None


def test_links_and_spam_in_text_are_refused(seeker):
    assert create(seeker, about="Звоните, мой сайт https://spam.example").status_code == 422
    assert create(seeker, place_1="http://spam.example").status_code == 422


def test_honeypot_pretends_success_and_saves_nothing(seeker):
    r = create(seeker, website="http://bot.example")
    assert r.status_code == 303
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0


def test_csrf_and_origin_are_required(seeker):
    r = seeker.client.post("/cabinet/resumes/new", data=form(), headers={"origin": "http://testserver"})
    assert r.status_code == 403
    token = re.search(r'name="csrf_token" value="([^"]+)"', seeker.client.get("/cabinet/resumes/new").text).group(1)
    r = seeker.client.post("/cabinet/resumes/new", data={"csrf_token": token, **form()}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0


def test_limit_of_three_resumes_in_ui(seeker):
    for _ in range(3):
        assert create(seeker).status_code == 303
    page = seeker.client.get("/cabinet/resumes")
    assert "3 резюме из 3" in page.text and 'href="/cabinet/resumes/new"' not in page.text
    assert seeker.client.get("/cabinet/resumes/new", follow_redirects=False).headers["location"] == "/cabinet/resumes?err=limit"
    r = create(seeker)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/resumes?err=limit"
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 3
    assert "это предел" in seeker.client.get("/cabinet/resumes?err=limit").text


def test_creation_rate_limit(tmp_path):
    e = build_env(tmp_path, rate_limits={"resume_create": (2, 3600)})
    with e.client:
        login_as(e, "a@example.test")
        assert create(e).status_code == 303
        assert create(e).status_code == 303
        assert create(e).status_code == 429


# ------------------------------------------------------------------ правка

def test_edit_form_prefilled_with_completeness(seeker):
    rid = create_id(seeker)
    r = seeker.client.get(f"/cabinet/resumes/{rid}/edit")
    assert r.status_code == 200
    for needle in ('value="Бариста"', 'value="30000"', 'value="Кофейня Зерно"', "кофе, касса, сервис", 'value="+79491112233"',
                   'name="consent" value="1" checked', "100%", f'action="/cabinet/resumes/{rid}/edit"'):
        assert needle in r.text, needle
    assert re.search(r'value="public"\s+checked', r.text)
    assert re.search(r'value="точка"\s+checked', r.text)


def test_edit_saves_changes_and_keeps_consent_time(seeker):
    rid = create_id(seeker)
    before = row(seeker, rid)
    r = submit(seeker, f"/cabinet/resumes/{rid}/edit", form(pay_min="45000", skills="кофе"))
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/resumes?done=saved"
    after = row(seeker, rid)
    assert after["pay_min"] == 45000 and after["consent_at"] == before["consent_at"]


def test_edit_error_shows_form_again(seeker):
    rid = create_id(seeker)
    r = submit(seeker, f"/cabinet/resumes/{rid}/edit", form(profession=""))
    assert r.status_code == 422 and "err-profession" in r.text
    assert row(seeker, rid)["title"] == "Бариста"


def test_other_user_cannot_touch_resume(env):
    w = make_world(env)
    rid = create_id(w.seeker)
    other = stranger(env)
    for method, path in (("get", f"/cabinet/resumes/{rid}/edit"), ("get", f"/cabinet/resumes/{rid}/delete")):
        assert getattr(other.client, method)(path).status_code == 404, path
    for action in ("edit", "hide", "refresh", "delete"):
        assert submit(other, f"/cabinet/resumes/{rid}/{action}", form()).status_code == 404, action
    assert row(env, rid)["title"] == "Бариста"
    assert "Бариста" not in other.client.get("/cabinet/resumes").text


def test_bad_ids_are_404(seeker):
    for rid in ("999", "abc", "-1", "1" * 30):
        assert seeker.client.get(f"/cabinet/resumes/{rid}/edit").status_code == 404, rid


# ------------------------------------------------------------------ скрыть, обновить, удалить

def test_hide_withdraws_consent_and_shows_hidden_state(seeker):
    rid = create_id(seeker)
    r = submit(seeker, f"/cabinet/resumes/{rid}/hide")
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/resumes?done=hidden"
    saved = row(seeker, rid)
    assert saved["visibility"] == "hidden" and saved["consent_withdrawn_at"]
    page = seeker.client.get(r.headers["location"]).text
    assert "Резюме скрыто" in page and "Показать работодателям" in page and f"/cabinet/resumes/{rid}/edit?show=1" in page


def test_show_again_asks_for_consent_again(seeker):
    rid = create_id(seeker)
    submit(seeker, f"/cabinet/resumes/{rid}/hide")
    page = seeker.client.get(f"/cabinet/resumes/{rid}/edit?show=1").text
    assert re.search(r'value="public"\s+checked', page) and 'name="consent" value="1" checked' not in page
    r = submit(seeker, f"/cabinet/resumes/{rid}/edit", form())
    assert r.status_code == 303
    saved = row(seeker, rid)
    assert saved["visibility"] == "public" and saved["consent_withdrawn_at"] is None


def test_refresh_updates_date(env):
    login_as(env, "r@example.test")
    rid = create_id(env)
    env.clock.advance(days=20)
    r = submit(env, f"/cabinet/resumes/{rid}/refresh")
    assert r.headers["location"] == "/cabinet/resumes?done=refreshed"
    assert row(env, rid)["updated_at"].startswith("2026-11-18")
    assert "Резюме обновлено" in env.client.get(r.headers["location"]).text


def test_stale_resume_is_explained(env):
    login_as(env, "r@example.test")
    create_id(env)
    conn = env.db()
    conn.execute("UPDATE resumes SET updated_at='2025-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    page = env.client.get("/cabinet/resumes").text
    assert "Давно не обновлялось" in page and "не обновлялось больше года" in page and "Обновить" in page


def test_delete_needs_confirmation_and_removes_everything(seeker):
    rid = create_id(seeker)
    page = seeker.client.get(f"/cabinet/resumes/{rid}/delete")
    assert page.status_code == 200 and "Удалить резюме" in page.text and "Скрыть вместо удаления" in page.text
    assert row(seeker, rid)
    r = submit(seeker, f"/cabinet/resumes/{rid}/delete")
    assert r.status_code == 303 and r.headers["location"] == "/cabinet/resumes?done=deleted"
    assert seeker.query("SELECT COUNT(*) AS n FROM resumes")[0]["n"] == 0
    assert "Резюме удалено" in seeker.client.get("/cabinet/resumes?done=deleted").text
    assert seeker.client.get(f"/cabinet/resumes/{rid}/edit").status_code == 404


def test_deleting_frees_a_slot(seeker):
    ids = [create_id(seeker) for _ in range(3)]
    submit(seeker, f"/cabinet/resumes/{ids[0]}/delete")
    assert create(seeker).status_code == 303


def test_admin_hidden_resume_shows_reason_and_cannot_be_reopened(env):
    login_as(env, "a@example.test")
    rid = create_id(env)
    conn = env.db()
    conn.execute("UPDATE resumes SET admin_hidden=1, admin_note='Чужие данные'")
    conn.commit()
    conn.close()
    page = env.client.get("/cabinet/resumes").text
    assert "Скрыто модератором" in page and "Чужие данные" in page
    assert f"/cabinet/resumes/{rid}/edit?show=1" not in page


def test_account_page_mentions_resumes_in_deletion_notice(seeker):
    assert "резюме" in seeker.client.get("/account").text


def test_cabinet_seeker_has_resume_block(seeker):
    page = seeker.client.get("/cabinet/seeker").text
    assert "data-cabinet-resumes" in page and 'href="/cabinet/resumes"' in page and "Составить резюме" in page
    create_id(seeker, pay_min="", skills="кофе")
    page = seeker.client.get("/cabinet/seeker").text
    assert "data-resume-count=\"1\"" in page and "Резюме из 3" in page


def test_cabinet_employer_links_to_search(env):
    login_as(env, "boss@example.test", role="employer")
    assert 'href="/employers/resumes"' in env.client.get("/cabinet/employer").text


def test_second_visitor_helper_keeps_cookies_apart(env):
    other = second_visitor(env)
    assert other.client is not env.client
