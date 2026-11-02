"""Публичные страницы: заявка на карточку, мои заявки, архив компании, кнопка «Это моя компания»."""
from __future__ import annotations

import re

import pytest

from tests.accounts.harness import build_env, code_in, csrf_of, login_as
from tests.employers_auto.kit import auto_company, vacancy

PHONE = "+79490001122"


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def cid(env):
    conn = env.db()
    try:
        return auto_company(conn, "Кофейня Зерно", count=3, contacts={"phones": [PHONE], "email": "hr@zerno.example"})
    finally:
        conn.close()


def robots(html: str) -> str:
    m = re.search(r'<meta name="robots" content="([^"]*)"', html)
    return m.group(1) if m else ""


def claim_post(env, cid, data, path=None, page=None):
    page = page or f"/company/{cid}/claim"
    token = csrf_of(env, page)
    return env.client.post(path or f"/company/{cid}/claim", data={"csrf_token": token, **data}, follow_redirects=False)


def db_rows(env, sql, *a):
    return env.query(sql, *a)


# ------------------------------------------------------------------ страница компании

def test_company_page_offers_claim_for_auto_card(env, cid):
    html = env.client.get(f"/company/{cid}").text
    assert f"/company/{cid}/claim" in html and "Это моя компания" in html


def test_claim_button_hidden_once_owned(env, cid):
    uid = login_as(env, "boss@example.test", role="employer")
    conn = env.db()
    conn.execute("UPDATE companies SET owner_user_id=? WHERE id=?", (uid, cid))
    conn.commit()
    conn.close()
    assert "Это моя компания" not in env.client.get(f"/company/{cid}").text


# ------------------------------------------------------------------ доступ

def test_guest_is_offered_login_and_register(env, cid):
    r = env.client.get(f"/company/{cid}/claim")
    assert r.status_code == 200
    assert "noindex" in robots(r.text)
    assert f"/login?next=/company/{cid}/claim" in r.text and "/register" in r.text
    assert 'name="phone"' not in r.text, "гостю форма заявки не показывается"


def test_guest_cannot_post(env, cid):
    r = claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"}, page="/login")
    assert r.status_code == 403
    assert not db_rows(env, "SELECT 1 FROM company_claims")


def test_job_seeker_sees_explanation_and_cannot_post(env, cid):
    login_as(env, "seeker@example.test", role="job_seeker")
    r = env.client.get(f"/company/{cid}/claim")
    assert r.status_code == 200 and "работодател" in r.text.lower()
    assert claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"}).status_code == 403


def test_unknown_blocked_and_merged_cards(env, cid):
    login_as(env, "boss@example.test", role="employer")
    assert env.client.get("/company/99999/claim").status_code == 404
    conn = env.db()
    other = auto_company(conn, "Пекарня Хлеб", count=1, source="@z")
    conn.execute("UPDATE companies SET blocked_at='2026-10-01' WHERE id=?", (other,))
    conn.execute("UPDATE companies SET merged_into=? WHERE id=?", (cid, auto_company(conn, "Зерно Кофе", count=1, source="@w")))
    conn.commit()
    merged = conn.execute("SELECT id FROM companies WHERE merged_into IS NOT NULL").fetchone()[0]
    conn.close()
    assert env.client.get(f"/company/{other}/claim").status_code == 404
    r = env.client.get(f"/company/{merged}/claim", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/company/{cid}/claim"


# ------------------------------------------------------------------ подача

def test_employer_phone_claim_goes_to_review(env, cid):
    login_as(env, "boss@example.test", role="employer")
    page = env.client.get(f"/company/{cid}/claim")
    assert page.status_code == 200 and 'name="method"' in page.text and "no-store" in page.headers["cache-control"]
    r = claim_post(env, cid, {"method": "phone", "phone": "8 949 000-11-22", "consent": "1", "message": "Я директор"})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/company/{cid}/claim")
    k = db_rows(env, "SELECT * FROM company_claims")[0]
    assert k["status"] == "review" and k["contact"] == PHONE
    after = env.client.get(r.headers["location"])
    assert "на проверке" in after.text.lower()
    assert env.mailer.outbox == [] or all("Код" not in m.subject for m in env.mailer.outbox)


def test_consent_is_required(env, cid):
    login_as(env, "boss@example.test", role="employer")
    r = claim_post(env, cid, {"method": "phone", "phone": PHONE})
    assert r.status_code == 422 and "согласие" in r.text.lower()
    assert not db_rows(env, "SELECT 1 FROM company_claims")


def test_wrong_phone_shows_error_and_keeps_input(env, cid):
    login_as(env, "boss@example.test", role="employer")
    r = claim_post(env, cid, {"method": "phone", "phone": "+79490009999", "consent": "1"})
    assert r.status_code == 422 and "нет в вакансиях" in r.text and "+79490009999" in r.text


def test_free_mail_rejected_on_page(env, cid):
    login_as(env, "boss@example.test", role="employer")
    r = claim_post(env, cid, {"method": "email", "email": "boss@mail.ru", "consent": "1"})
    assert r.status_code == 422 and "рабочая почта" in r.text.lower()


def test_email_flow_with_code(env, cid):
    login_as(env, "boss@example.test", role="employer")
    r = claim_post(env, cid, {"method": "email", "email": "boss@zerno.example", "consent": "1"})
    assert r.status_code == 303
    mail = [m for m in env.mailer.outbox if m.to == "boss@zerno.example"][-1]
    code = code_in(mail)
    assert db_rows(env, "SELECT status FROM company_claims")[0][0] == "sent"
    bad = claim_post(env, cid, {"code": "000000" if code != "000000" else "111111"}, f"/company/{cid}/claim/code")
    assert bad.status_code == 422 and "код" in bad.text.lower()
    ok = claim_post(env, cid, {"code": code}, f"/company/{cid}/claim/code")
    assert ok.status_code == 303
    assert db_rows(env, "SELECT status FROM company_claims")[0][0] == "review"


def test_resend_has_cooldown(env, cid):
    login_as(env, "boss@example.test", role="employer")
    claim_post(env, cid, {"method": "email", "email": "boss@zerno.example", "consent": "1"})
    r = claim_post(env, cid, {}, f"/company/{cid}/claim/resend")
    assert r.status_code == 429
    env.clock.advance(seconds=61)
    r = claim_post(env, cid, {}, f"/company/{cid}/claim/resend")
    assert r.status_code == 303
    assert len([m for m in env.mailer.outbox if m.to == "boss@zerno.example"]) == 2


def test_note_and_cancel(env, cid):
    login_as(env, "boss@example.test", role="employer")
    claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"})
    assert claim_post(env, cid, {"note": " "}, f"/company/{cid}/claim/note").status_code == 422
    assert claim_post(env, cid, {"note": "ИНН 9300000001"}, f"/company/{cid}/claim/note").status_code == 303
    assert "ИНН 9300000001" in db_rows(env, "SELECT message FROM company_claims")[0][0]
    assert claim_post(env, cid, {}, f"/company/{cid}/claim/cancel").status_code == 303
    assert db_rows(env, "SELECT status FROM company_claims")[0][0] == "rejected"


def test_second_claim_while_open_is_refused(env, cid):
    login_as(env, "boss@example.test", role="employer")
    claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"})
    r = claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"})
    assert r.status_code in (200, 409) and len(db_rows(env, "SELECT 1 FROM company_claims")) == 1


def test_post_needs_csrf(env, cid):
    login_as(env, "boss@example.test", role="employer")
    r = env.client.post(f"/company/{cid}/claim", data={"method": "phone", "phone": PHONE, "consent": "1"})
    assert r.status_code == 403
    r = env.client.post(f"/company/{cid}/claim", data={"csrf_token": "0" * 64, "method": "phone", "phone": PHONE, "consent": "1"})
    assert r.status_code == 403
    assert not db_rows(env, "SELECT 1 FROM company_claims")


def test_ip_rate_limit(env, cid):
    login_as(env, "boss@example.test", role="employer")
    codes = []
    for _ in range(12):
        codes.append(claim_post(env, cid, {"method": "phone", "phone": "+79490009999", "consent": "1"}).status_code)
    assert 429 in codes


# ------------------------------------------------------------------ мои заявки

def test_my_claims_page(env, cid):
    assert env.client.get("/cabinet/claims", follow_redirects=False).status_code == 303
    login_as(env, "boss@example.test", role="employer")
    empty = env.client.get("/cabinet/claims")
    assert empty.status_code == 200 and "noindex" in robots(empty.text)
    claim_post(env, cid, {"method": "phone", "phone": PHONE, "consent": "1"})
    page = env.client.get("/cabinet/claims")
    assert "Кофейня Зерно" in page.text and "На проверке" in page.text


def test_my_claims_forbidden_for_seeker(env):
    login_as(env, "seeker@example.test", role="job_seeker")
    assert env.client.get("/cabinet/claims").status_code == 403


# ------------------------------------------------------------------ архив

def test_archive_page_lists_closed_vacancies_and_is_noindex(env, cid):
    conn = env.db()
    old = vacancy(conn, "Кофейня Зерно", profession="кондитер", external_id="arch1", pay_min=30000, pay_max=40000, pay_period="месяц")
    conn.execute("UPDATE vacancies SET status='archived', company_id=? WHERE id=?", (cid, old))
    conn.commit()
    conn.close()
    r = env.client.get(f"/company/{cid}/archive")
    assert r.status_code == 200
    assert "noindex" in robots(r.text)
    text = r.text.replace("\xa0", " ").replace("\u202f", " ")
    assert "Кондитер" in text and "30 000" in text and "29 октября" in text
    assert f"/vacancy/{old}" not in r.text, "закрытые вакансии не ведут на страницу 410"
    page = env.client.get(f"/company/{cid}")
    assert f"/company/{cid}/archive" in page.text


def test_archive_page_empty_and_missing(env, cid):
    r = env.client.get(f"/company/{cid}/archive")
    assert r.status_code == 200 and "noindex" in robots(r.text)
    assert env.client.get("/company/99999/archive").status_code == 404
    assert env.client.get(f"/company/{cid}/archive?page=abc").status_code == 200


def test_archived_vacancy_page_is_closed_and_noindex(env, cid):
    conn = env.db()
    vid = conn.execute("SELECT id FROM vacancies WHERE company_id=? LIMIT 1", (cid,)).fetchone()[0]
    conn.execute("UPDATE vacancies SET status='archived' WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    r = env.client.get(f"/vacancy/{vid}")
    assert r.status_code == 410 and "noindex" in robots(r.text)
    assert f"/vacancy/{vid}</loc>" not in env.client.get("/sitemap.xml").text
