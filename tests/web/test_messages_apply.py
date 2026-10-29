"""Отклик на вакансию работодателя через сайт."""
import pytest

from rabota.accounts.mailer import MemoryMailer
from tests.accounts.harness import build_env, env_impl
from tests.messaging.webkit import apply_as_seeker, make_world, new_vacancy, second_visitor, stranger, submit

env = pytest.fixture(env_impl)


def test_guest_is_sent_to_login_and_back(env):
    vid = new_vacancy(env, None, origin="direct")
    r = env.client.get(f"/vacancy/{vid}/apply", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/vacancy/{vid}/apply"


def test_employer_cannot_respond_and_is_told_why(env):
    w = make_world(env)
    r = w.boss.client.get(f"/vacancy/{w.vacancy}/apply")
    assert r.status_code == 403 and "Откликаться могут только соискатели" in r.text


def test_apply_page_for_seeker(env):
    w = make_world(env)
    r = w.seeker.client.get(f"/vacancy/{w.vacancy}/apply")
    assert r.status_code == 200
    for needle in (f'action="/vacancy/{w.vacancy}/apply"', 'name="cover"', 'name="share_phone"', 'name="csrf_token"',
                   "Бариста", "+7 949 111-22-33"):
        assert needle in r.text, needle
    assert 'name="robots" content="noindex' in r.text and '<link rel="canonical"' in r.text
    assert "<title>" in r.text and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kw", [{"origin": "parsed"}, {"status": "expired"}, {"moderation": "pending"}])
def test_apply_page_404_for_vacancies_without_responses(env, kw):
    w = make_world(env)
    vid = new_vacancy(env, w.boss_id, **kw)
    assert w.seeker.client.get(f"/vacancy/{vid}/apply").status_code == 404
    assert submit(w.seeker, f"/vacancy/{vid}/apply", {"cover": "Привет"}).status_code == 404
    assert w.seeker.client.get("/vacancy/99999/apply").status_code == 404


def test_apply_creates_response_and_thread_and_mails_employer(env):
    w = make_world(env)
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Здравствуйте, я бариста", "share_phone": "1"})
    assert r.status_code == 303
    rid = int(r.headers["location"].split("/responses/")[1].split("?")[0])
    assert r.headers["location"].endswith("?sent=1")
    app = env.query("SELECT * FROM applications")[0]
    assert (app["id"], app["user_id"], app["vacancy_id"], app["contact_phone"]) == (rid, w.seeker_id, w.vacancy, "+79491112233")
    assert env.query("SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 1
    mails = [m for m in env.mailer.outbox if m.to == "boss@example.test"]
    assert len(mails) == 1
    body = mails[0].subject + mails[0].body
    assert f"/responses/{rid}" in body and "бариста" in mails[0].subject
    for private in ("Мария", "949", "seeker@example.test", "я бариста"):
        assert private not in body
    page = w.seeker.client.get(r.headers["location"])
    assert "Отклик отправлен" in page.text


def test_phone_not_shared_when_box_is_unchecked(env):
    w = make_world(env)
    submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": ""})
    assert env.query("SELECT contact_phone FROM applications")[0]["contact_phone"] is None


def test_second_response_is_refused_and_sends_no_mail(env):
    w = make_world(env)
    apply_as_seeker(w)
    before = len(env.mailer.outbox)
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Ещё раз"})
    assert r.status_code == 409 and "Вы уже откликались на эту вакансию" in r.text
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 1
    assert len(env.mailer.outbox) == before
    again = w.seeker.client.get(f"/vacancy/{w.vacancy}/apply")
    assert "Вы уже откликались на эту вакансию" in again.text and 'name="cover"' not in again.text


def test_csrf_is_required(env):
    w = make_world(env)
    r = w.seeker.client.post(f"/vacancy/{w.vacancy}/apply", data={"cover": "Привет"})
    assert r.status_code == 403
    r2 = w.seeker.client.post(f"/vacancy/{w.vacancy}/apply", data={"cover": "Привет", "csrf_token": "forged"})
    assert r2.status_code == 403
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_foreign_origin_is_refused(env):
    w = make_world(env)
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет"}, headers={"origin": "https://evil.test"})
    assert r.status_code == 403
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_guest_and_employer_posts_do_not_create_responses(env):
    w = make_world(env)
    guest = second_visitor(env)
    token_page = guest.client.get("/login").text
    assert "csrf_token" in token_page
    r = guest.client.post(f"/vacancy/{w.vacancy}/apply", data={"cover": "x"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert submit(w.boss, f"/vacancy/{w.vacancy}/apply", {"cover": "x"}).status_code == 403
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_cover_too_long_shows_error_and_keeps_text(env):
    w = make_world(env)
    text = "а" * 2001
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": text, "share_phone": "1"})
    assert r.status_code == 422 and "слишком длинное" in r.text.lower() and 'role="alert"' in r.text
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_honeypot_pretends_success_without_saving(env):
    w = make_world(env)
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет", "website": "http://spam.test"})
    assert r.status_code == 303
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


def test_response_rate_limit_per_user(tmp_path):
    env = build_env(tmp_path, rate_limits={"apply_user": (2, 3600)})
    with env.client:
        w = make_world(env)
        for i in range(2):
            vid = new_vacancy(env, w.boss_id, profession=f"профессия {i}")
            assert submit(w.seeker, f"/vacancy/{vid}/apply", {"cover": ""}).status_code == 303
        vid = new_vacancy(env, w.boss_id, profession="третья")
        r = submit(w.seeker, f"/vacancy/{vid}/apply", {"cover": ""})
        assert r.status_code == 429
        assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 2


def test_mail_failure_does_not_break_the_response(tmp_path):
    env = build_env(tmp_path, mailer=MemoryMailer())
    with env.client:
        w = make_world(env)
        env.mailer.fail = True
        r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет"})
        assert r.status_code == 303
        assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 1


def test_employer_who_blocked_seeker_gets_no_response(env):
    w = make_world(env)
    env.db().execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?,?)", (w.boss_id, w.seeker_id)).connection.commit()
    r = submit(w.seeker, f"/vacancy/{w.vacancy}/apply", {"cover": "Привет"})
    assert r.status_code == 422 and "заблок" not in r.text.lower()
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 0


# ------------------------------------------------------------------ блок отклика на странице вакансии

def test_vacancy_page_for_guest_offers_login(env):
    vid = new_vacancy(env, None, origin="direct")
    html = env.client.get(f"/vacancy/{vid}").text
    assert f'href="/vacancy/{vid}/apply"' in html and "Для отклика нужен вход" in html
    r = env.client.get(f"/vacancy/{vid}/apply", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_vacancy_page_shows_form_to_seeker_then_state_after_response(env):
    w = make_world(env)
    html = w.seeker.client.get(f"/vacancy/{w.vacancy}").text
    assert f'action="/vacancy/{w.vacancy}/apply"' in html and 'name="cover"' in html
    apply_as_seeker(w)
    html = w.seeker.client.get(f"/vacancy/{w.vacancy}").text
    assert "Вы откликнулись" in html and 'name="cover"' not in html and "/responses/" in html


def test_vacancy_page_for_employer_has_no_form(env):
    w = make_world(env)
    html = w.boss.client.get(f"/vacancy/{w.vacancy}").text
    assert 'name="cover"' not in html and "Откликаться могут только соискатели" in html


def test_parsed_vacancy_keeps_call_buttons_and_has_no_response_box(env):
    w = make_world(env)
    vid = new_vacancy(env, None, origin="parsed")
    html = w.seeker.client.get(f"/vacancy/{vid}").text
    assert 'name="cover"' not in html and 'id="apply"' not in html and "Связаться" in html


def test_other_seeker_can_still_respond_to_same_vacancy(env):
    w = make_world(env)
    apply_as_seeker(w)
    other = stranger(env, "other@example.test")
    assert submit(other, f"/vacancy/{w.vacancy}/apply", {"cover": "Я тоже"}).status_code == 303
    assert env.query("SELECT COUNT(*) AS n FROM applications")[0]["n"] == 2
