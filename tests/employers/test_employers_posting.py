"""Размещение вакансии за минуту: гость вводит почту, подтверждает кодом, аккаунт создаётся сам."""
from __future__ import annotations

import re

import pytest

from tests.accounts.harness import PASSWORD, build_env, code_in, login_as, make_user, wrong_code
from tests.web.postkit import GUEST_EMAIL, Poster, csrf

FORM = {
    "profession": "Бариста", "district": "Ворошиловский", "pay_min": "30000", "pay_max": "40000", "pay_period": "месяц",
    "schedule": "2/2", "phone": "+7 949 000-00-77", "summary": "Готовить кофе и общаться с гостями, обучим с нуля.",
    "employer": "Кофейня Зерно",
}


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path, rate_limits={"post": (100, 3600), "post_code_email": (100, 3600)})
    with e.client:
        yield e


@pytest.fixture
def p(env):
    return Poster(env.client, env.mailer)


def count(env, table="vacancies") -> int:
    return env.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def start(p, form=FORM, email=GUEST_EMAIL):
    r = p.submit(form, email)
    return p.token_of(r)


# ------------------------------------------------------------------ форма

def test_form_is_short_and_has_consent_and_email(env):
    html = env.client.get("/razmestit").text
    for name in ("profession", "district", "pay_min", "pay_max", "schedule", "phone", "summary", "account_email", "consent"):
        assert f'name="{name}"' in html
    assert "<h1" in html and html.count("<h1") == 1
    assert "обработк" in html.lower()  # согласие на обработку данных


def test_guest_needs_email_and_consent(env, p):
    r = p.submit(FORM, "не почта")
    assert r.status_code == 422 and 'id="err-account_email"' in r.text
    r = p.submit(FORM, consent=False)
    assert r.status_code == 422 and 'id="err-consent"' in r.text
    assert env.mailer.outbox == [] and count(env) == 0 and count(env, "post_drafts") == 0


def test_post_requires_csrf_and_same_origin(env, p):
    data = {"account_email": GUEST_EMAIL, "consent": "1", **FORM}
    assert env.client.post("/razmestit", data=data, headers={"Origin": "http://testserver"}).status_code == 403
    data["csrf_token"] = csrf(env.client)
    assert env.client.post("/razmestit", data=data, headers={"Origin": "https://evil.example"}).status_code == 403
    assert count(env, "post_drafts") == 0


def test_posts_from_one_address_are_limited(env, p):
    env.app.state.employer_hooks.limiter.limits["post"] = (2, 3600)
    assert [p.submit(FORM, f"a{i}@example.test").status_code for i in range(3)] == [303, 303, 429]


def test_honeypot_pretends_success(env, p):
    r = p.submit({**FORM, "website": "http://spam"})
    assert r.status_code == 303 and r.headers["location"] == "/razmestit/spasibo"
    assert env.mailer.outbox == [] and count(env, "post_drafts") == 0


def test_category_is_guessed_and_period_defaulted(env, p):
    form = {k: v for k, v in FORM.items() if k != "pay_period"}
    p.confirm(start(p, form))
    v = env.query("SELECT category, pay_period FROM vacancies")[0]
    assert v["category"] == "общепит" and v["pay_period"] == "месяц"


# ------------------------------------------------------------------ код и создание аккаунта

def test_code_mail_and_nothing_published_before_code(env, p):
    token = start(p)
    mail = env.mailer.last(GUEST_EMAIL)
    assert mail and code_in(mail) in mail.subject
    assert count(env) == 0 and env.user(GUEST_EMAIL) is None  # ни вакансии, ни аккаунта до кода
    page = env.client.get(f"/razmestit/kod/{token}")
    assert page.status_code == 200 and "noindex" in page.text and GUEST_EMAIL not in page.text  # адрес скрыт
    assert 'name="code"' in page.text


def test_right_code_creates_employer_company_vacancy_and_session(env, p):
    token = start(p)
    r = p.confirm(token)
    assert r.status_code == 303 and re.fullmatch(r"/razmestit/gotovo/\d+\?new=1", r.headers["location"])
    user = env.user(GUEST_EMAIL)
    assert user["role"] == "employer" and user["email_verified_at"] and user["consent_at"]
    company = env.query("SELECT * FROM companies")[0]
    assert company["owner_user_id"] == user["id"] and company["name"] == "Кофейня Зерно"
    v = env.query("SELECT * FROM vacancies")[0]
    assert v["owner_user_id"] == user["id"] and v["company_id"] == company["id"] and v["origin"] == "direct"
    assert (v["moderation_status"], v["status"]) == ("pending", "hidden")
    ready = env.client.get(r.headers["location"])
    assert ready.status_code == 200 and "Вакансия принята" in ready.text and "Мы создали ваш аккаунт" in ready.text
    assert "Бариста" in env.client.get("/cabinet/vacancies").text  # сессия открыта


def test_account_has_no_known_password(env, p):
    p.confirm(start(p))
    env.client.cookies.clear()
    from tests.accounts.harness import login
    assert login(env, GUEST_EMAIL, PASSWORD).status_code != 303


def test_code_cannot_be_used_twice(env, p):
    token = start(p)
    code = code_in(env.mailer.last(GUEST_EMAIL))
    assert p.confirm(token, code).status_code == 303
    assert p.confirm(token, code).status_code == 410
    assert count(env) == 1


def test_wrong_code_is_refused_and_locks_after_five(env, p):
    token = start(p)
    code = code_in(env.mailer.last(GUEST_EMAIL))
    bad = wrong_code(code)
    for _ in range(4):
        assert p.confirm(token, bad).status_code == 400
    r = p.confirm(token, bad)
    assert r.status_code in (400, 429)
    locked = p.confirm(token, code)  # даже верный код после блокировки не принимается
    assert locked.status_code == 429 and count(env) == 0 and env.user(GUEST_EMAIL) is None


def test_bad_format_does_not_burn_attempts(env, p):
    token = start(p)
    code = code_in(env.mailer.last(GUEST_EMAIL))
    for _ in range(8):
        assert p.confirm(token, "12ab").status_code == 400
    assert p.confirm(token, code).status_code == 303


def test_expired_code(env, p):
    token = start(p)
    code = code_in(env.mailer.last(GUEST_EMAIL))
    env.clock.advance(minutes=16)
    r = p.confirm(token, code)
    assert r.status_code == 400 and "Срок кода вышел" in r.text
    assert count(env) == 0


def test_unknown_and_old_tokens_are_gone(env, p):
    assert env.client.get("/razmestit/kod/not-a-real-token").status_code == 410
    token = start(p)
    env.clock.advance(days=2)
    assert env.client.get(f"/razmestit/kod/{token}").status_code == 410


def test_code_post_requires_csrf(env, p):
    token = start(p)
    code = code_in(env.mailer.last(GUEST_EMAIL))
    r = env.client.post(f"/razmestit/kod/{token}", data={"code": code})
    assert r.status_code == 403 and count(env) == 0


def test_resend_cooldown_and_new_code(env, p):
    token = start(p)
    old = code_in(env.mailer.last(GUEST_EMAIL))
    n = len(env.mailer.outbox)

    def resend():
        data = {"csrf_token": csrf(env.client, f"/razmestit/kod/{token}")}
        return env.client.post(f"/razmestit/kod/{token}/povtor", data=data)

    assert "через минуту" in resend().text and len(env.mailer.outbox) == n  # слишком рано
    env.clock.advance(seconds=61)
    assert "новый код" in resend().text and len(env.mailer.outbox) == n + 1
    new = code_in(env.mailer.last(GUEST_EMAIL))
    assert new != old
    assert p.confirm(token, old).status_code == 400  # старый код не действует (если случайно не совпал)
    assert p.confirm(token, new).status_code == 303


def test_resend_has_a_limit(env, p):
    token = start(p)
    for _ in range(4):
        env.clock.advance(seconds=61)
        data = {"csrf_token": csrf(env.client, f"/razmestit/kod/{token}")}
        env.client.post(f"/razmestit/kod/{token}/povtor", data=data)
    env.clock.advance(seconds=61)
    data = {"csrf_token": csrf(env.client, f"/razmestit/kod/{token}")}
    r = env.client.post(f"/razmestit/kod/{token}/povtor", data=data)
    assert r.status_code == 429 and len(env.mailer.outbox) == 5


def test_email_gets_limited_number_of_codes(tmp_path):
    e = build_env(tmp_path, rate_limits={"post": (100, 3600)})
    e.app.state.employer_hooks.limiter.limits["post"] = (100, 3600)  # лимит по адресу посетителя тут не мешает
    with e.client:
        pp = Poster(e.client, e.mailer)
        codes = [pp.submit(FORM).status_code for _ in range(6)]
        assert codes == [303] * 5 + [429]
        assert len(e.mailer.outbox) == 5


def test_wrong_codes_from_one_ip_are_limited(tmp_path):
    e = build_env(tmp_path, rate_limits={"post": (100, 3600), "post_code_fail_ip": (3, 3600)})
    with e.client:
        pp = Poster(e.client, e.mailer)
        token = pp.token_of(pp.submit(FORM))
        assert [pp.confirm(token, "000000").status_code for _ in range(4)] == [400, 400, 400, 429]


# ------------------------------------------------------------------ существующие аккаунты

def test_existing_employer_gets_vacancy_in_own_company(env, p):
    uid = make_user(env, GUEST_EMAIL, role="employer", name="Олег")
    r = p.confirm(start(p))
    assert r.headers["location"].endswith(re.search(r"/gotovo/\d+", r.headers["location"]).group(0))
    assert "?new=1" not in r.headers["location"]
    assert env.query("SELECT owner_user_id FROM vacancies")[0]["owner_user_id"] == uid
    assert len(env.query("SELECT id FROM companies")) == 1


def test_seeker_email_stays_anonymous_and_not_signed_in(env, p):
    make_user(env, "anna@example.test", role="job_seeker")
    r = p.confirm(start(p, email="anna@example.test"), email="anna@example.test")
    assert r.headers["location"] == "/razmestit/spasibo"
    assert env.query("SELECT owner_user_id, company_id FROM vacancies")[0]["owner_user_id"] is None
    assert env.client.get("/cabinet/vacancies", follow_redirects=False).status_code in (303, 401, 403)  # сессии нет


def test_blocked_user_cannot_post(env, p):
    uid = make_user(env, GUEST_EMAIL, role="employer")
    conn = env.db()
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    r = p.confirm(start(p))
    assert r.status_code == 403 and count(env) == 0


def test_unverified_account_is_taken_over_only_by_code_owner(env, p):
    victim = make_user(env, GUEST_EMAIL, role="job_seeker", verified=False)
    token = start(p)
    assert env.query("SELECT email_verified_at FROM users WHERE id=?", victim)[0]["email_verified_at"] is None
    r = p.confirm(token)
    assert r.status_code == 303
    user = env.user(GUEST_EMAIL)
    assert user["role"] == "employer" and user["email_verified_at"]
    from tests.accounts.harness import login
    env.client.cookies.clear()
    assert login(env, GUEST_EMAIL, PASSWORD).status_code != 303  # прежний пароль больше не работает


def test_logged_in_employer_publishes_without_code(env):
    login_as(env, GUEST_EMAIL, role="employer", name="Олег")
    data = {"csrf_token": csrf(env.client), **FORM}
    r = env.client.post("/razmestit", data=data, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/razmestit/gotovo/")
    assert env.mailer.outbox == [] or all("Код" not in m.subject for m in env.mailer.outbox)


# ------------------------------------------------------------------ автомодерация при размещении

def test_stoplist_phone_rejects_at_once(env, p):
    from rabota.admin import stoplist
    conn = env.db()
    stoplist.add(conn, "phone", "+79490000077")
    conn.close()
    r = p.confirm(start(p))
    ready = env.client.get(r.headers["location"])
    assert env.query("SELECT moderation_status FROM vacancies")[0]["moderation_status"] == "rejected"
    assert "не принята" in ready.text


def test_suspicious_text_goes_to_manual_review(env, p):
    r = p.confirm(start(p, {**FORM, "summary": "Нужна предоплата за оформление, остальное просто."}))
    v = env.query("SELECT moderation_status, automod_flags FROM vacancies")[0]
    assert v["moderation_status"] == "pending" and "money" in v["automod_flags"]
    assert "проверят вручную" in env.client.get(r.headers["location"]).text


def test_verified_company_is_published_at_once(env, p):
    uid = make_user(env, GUEST_EMAIL, role="employer", name="Олег")
    conn = env.db()
    conn.execute("INSERT INTO companies(owner_user_id, name, verified_at) VALUES (?, 'Кофейня Зерно', '2026-10-01T10:00:00+00:00')", (uid,))
    conn.commit()
    conn.close()
    r = p.confirm(start(p))
    v = env.query("SELECT moderation_status, status, id FROM vacancies")[0]
    assert (v["moderation_status"], v["status"]) == ("approved", "active")
    assert "Вакансия опубликована" in env.client.get(r.headers["location"]).text
    assert env.client.get(f"/vacancy/{v['id']}").status_code == 200


# ------------------------------------------------------------------ страница «Готово» и «Поднять»

def published(env, p):
    uid = make_user(env, GUEST_EMAIL, role="employer", name="Олег")
    conn = env.db()
    conn.execute("INSERT INTO companies(owner_user_id, name, verified_at) VALUES (?, 'Кофейня Зерно', '2026-10-01T10:00:00+00:00')", (uid,))
    conn.commit()
    conn.close()
    r = p.confirm(start(p))
    vid = int(re.search(r"/gotovo/(\d+)", r.headers["location"]).group(1))
    stamp = env.clock().strftime("%Y-%m-%dT%H:%M:%S+00:00")
    conn = env.db()
    conn.execute("UPDATE vacancies SET published_at=?, last_seen_at=? WHERE id=?", (stamp, stamp, vid))
    conn.commit()
    conn.close()
    return vid


def post_raise(env, vid):
    data = {"csrf_token": csrf(env.client, f"/razmestit/gotovo/{vid}")}
    return env.client.post(f"/razmestit/gotovo/{vid}/podnyat", data=data, follow_redirects=False)


def test_ready_page_shows_tips_and_disabled_raise_when_too_soon(env, p):
    vid = published(env, p)
    html = env.client.get(f"/razmestit/gotovo/{vid}").text
    assert "Как получить больше откликов" in html and "data-raise-wait" in html and "noindex" in html


def test_raise_is_limited_to_once_a_day(env, p):
    vid = published(env, p)
    r = post_raise(env, vid)
    assert r.headers["location"].endswith("err=too_soon")
    assert "раз в сутки" in env.client.get(r.headers["location"]).text
    env.clock.advance(hours=25)
    r = post_raise(env, vid)
    assert r.headers["location"].endswith("done=raised")
    row = env.query("SELECT raise_count, raised_at FROM vacancies WHERE id=?", vid)[0]
    assert row["raise_count"] == 1 and row["raised_at"]
    assert post_raise(env, vid).headers["location"].endswith("err=too_soon")


def test_raise_requires_csrf_and_owner(env, p):
    vid = published(env, p)
    assert env.client.post(f"/razmestit/gotovo/{vid}/podnyat").status_code == 403
    env.client.cookies.clear()
    r = env.client.get(f"/razmestit/gotovo/{vid}", follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    login_as(env, "other@example.test", role="employer", name="Чужой")
    assert env.client.get(f"/razmestit/gotovo/{vid}").status_code == 404
    assert post_raise_other(env, vid) == 404


def post_raise_other(env, vid):
    data = {"csrf_token": csrf(env.client, "/account")}
    return env.client.post(f"/razmestit/gotovo/{vid}/podnyat", data=data, follow_redirects=False).status_code
