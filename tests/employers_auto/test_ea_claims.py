"""Заявки «Это моя компания»: подача, проверка контакта, код, лимиты, спор, решение администратора, удаление аккаунта."""
from __future__ import annotations

from datetime import timedelta

import pytest

from rabota.accounts import service
from rabota.employers_auto import claims
from tests.employers_auto.kit import NOW, SECRET_BYTES, SITE, auto_company, company, submit_claim
from tests.messaging.conftest import add_user

ADMIN = {"id": 999, "email": "admin@example.test", "role": "admin"}


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "claims.db"
    service.ensure_schema(path)
    c = service.open_db(path)
    yield c
    c.close()


@pytest.fixture
def boss(conn):
    return add_user(conn, "boss@example.test", "employer", "Олег Босс")


@pytest.fixture
def rival(conn):
    return add_user(conn, "rival@example.test", "employer", "Игорь Соперник")


@pytest.fixture
def cid(conn):
    return auto_company(conn, "Кофейня Зерно", count=2, contacts={"phones": ["+79490001122"], "email": "hr@zerno.example"})


def err(code, fn, *a, **kw):
    with pytest.raises(claims.ClaimError) as info:
        fn(*a, **kw)
    assert info.value.code == code
    return info.value


def rows(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


# ------------------------------------------------------------------ контакт

def test_phone_normalization():
    assert claims.normalize_phone("8 (949) 000-11-22") == "+79490001122"
    assert claims.normalize_phone("+7 949 000 11 22") == "+79490001122"
    assert claims.normalize_phone("12345") is None


def test_company_phones_and_domains_come_from_vacancies(conn, cid):
    c = company(conn, cid)
    assert "+79490001122" in claims.company_phones(conn, c)
    assert "zerno.example" in claims.company_domains(conn, c)


def test_phone_claim_goes_to_review(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="phone", contact="8 949 000-11-22")
    assert out.status == "review" and out.mail is None
    k = claims.get(conn, out.claim_id)
    assert k["phone_match"] == 1 and k["contact"] == "+79490001122" and k["dispute"] == 0


def test_phone_not_in_vacancies_is_refused(conn, cid, boss):
    err("phone_unknown", submit_claim, conn, cid, boss["id"], method="phone", contact="+79490009999")
    err("phone", submit_claim, conn, cid, boss["id"], method="phone", contact="abc")


def test_free_mail_is_refused(conn, cid, boss):
    for addr in ("boss@mail.ru", "boss@gmail.com", "boss@yandex.ru"):
        err("free_mail", submit_claim, conn, cid, boss["id"], method="email", contact=addr)
    err("email", submit_claim, conn, cid, boss["id"], method="email", contact="not-an-email")


def test_foreign_domain_is_refused_when_company_domain_known(conn, cid, boss):
    err("domain", submit_claim, conn, cid, boss["id"], method="email", contact="boss@other.example")


def test_email_claim_sends_code_and_goes_to_review_after_right_code(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="Boss@Zerno.example")
    assert out.status == "sent" and out.mail.to == "boss@zerno.example"
    code = out.mail.body.split(": ")[1][:6]
    k = claims.get(conn, out.claim_id)
    assert k["domain_match"] == 1 and k["code_hash"] and code not in k["code_hash"]
    assert claims.check_code(conn, SECRET_BYTES, out.claim_id, boss["id"], "000", NOW) == "format"
    assert claims.check_code(conn, SECRET_BYTES, out.claim_id, boss["id"], code, NOW + timedelta(minutes=1)) == "ok"
    k = claims.get(conn, out.claim_id)
    assert k["status"] == "review" and k["code_ok_at"] and k["code_hash"] is None


def test_code_wrong_locks_after_five_attempts(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="boss@zerno.example")
    code = out.mail.body.split(": ")[1][:6]
    wrong = "000000" if code != "000000" else "111111"
    results = [claims.check_code(conn, SECRET_BYTES, out.claim_id, boss["id"], wrong, NOW) for _ in range(5)]
    assert results[:4] == ["bad"] * 4 and results[4] == "locked"
    assert claims.check_code(conn, SECRET_BYTES, out.claim_id, boss["id"], code, NOW) == "locked", "верный код после блокировки не помогает"


def test_code_expires_after_15_minutes(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="boss@zerno.example")
    code = out.mail.body.split(": ")[1][:6]
    assert claims.check_code(conn, SECRET_BYTES, out.claim_id, boss["id"], code, NOW + timedelta(minutes=16)) == "expired"


def test_code_is_bound_to_the_applicant(conn, cid, boss, rival):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="boss@zerno.example")
    code = out.mail.body.split(": ")[1][:6]
    assert claims.check_code(conn, SECRET_BYTES, out.claim_id, rival["id"], code, NOW) == "none"


def test_resend_cooldown_and_cap(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="boss@zerno.example")
    err("cooldown", claims.resend_code, conn, SECRET_BYTES, out.claim_id, boss["id"], SITE, NOW + timedelta(seconds=10))
    t = NOW
    for _ in range(claims.MAX_CODES_PER_CLAIM - 1):
        t += timedelta(seconds=61)
        again = claims.resend_code(conn, SECRET_BYTES, out.claim_id, boss["id"], SITE, t)
        assert again.mail is not None
    t += timedelta(seconds=61)
    err("limit", claims.resend_code, conn, SECRET_BYTES, out.claim_id, boss["id"], SITE, t)


# ------------------------------------------------------------------ права и лимиты

def test_only_employer_can_claim(conn, cid):
    seeker = add_user(conn, "s@example.test", "job_seeker", "Мария")
    err("role", submit_claim, conn, cid, seeker["id"])


def test_blocked_or_merged_card_cannot_be_claimed(conn, cid, boss):
    conn.execute("UPDATE companies SET blocked_at=? WHERE id=?", (NOW.isoformat(), cid))
    conn.commit()
    err("gone", submit_claim, conn, cid, boss["id"])


def test_second_open_claim_by_same_user_refused(conn, cid, boss):
    submit_claim(conn, cid, boss["id"])
    err("open", submit_claim, conn, cid, boss["id"])


def test_open_claims_cap_and_daily_cap(conn, boss):
    ids = [auto_company(conn, f"Компания Номер{n}x", 1, source=f"@s{n}", contacts={"phones": ["+79490001122"]}) for n in range(5)]
    for c in ids[:3]:
        submit_claim(conn, c, boss["id"])
    err("open_max", submit_claim, conn, ids[3], boss["id"])


def test_daily_cap(conn, boss):
    from rabota.admin import settings
    settings.save(conn, ADMIN, {"claims_open_max": "10", "claims_per_day": "3"}, now=NOW)
    ids = [auto_company(conn, f"Компания Номер{n}x", 1, source=f"@s{n}", contacts={"phones": ["+79490001122"]}) for n in range(5)]
    for c in ids[:3]:
        submit_claim(conn, c, boss["id"])
    err("per_day", submit_claim, conn, ids[3], boss["id"])
    out = submit_claim(conn, ids[3], boss["id"], now=NOW + timedelta(days=1, minutes=1))
    assert out.status == "review"


def test_rival_claim_makes_a_dispute(conn, cid, boss, rival):
    first = submit_claim(conn, cid, boss["id"])
    second = submit_claim(conn, cid, rival["id"])
    assert claims.get(conn, first.claim_id)["dispute"] == 1 and claims.get(conn, second.claim_id)["dispute"] == 1


def test_claim_on_owned_company_is_a_dispute_and_needs_transfer(conn, cid, boss, rival):
    submit_claim(conn, cid, boss["id"])
    claims.approve(conn, 1, ADMIN, "проверено", "127.0.0.1", SITE, NOW)
    assert company(conn, cid)["owner_user_id"] == boss["id"]
    out = submit_claim(conn, cid, rival["id"])
    assert claims.get(conn, out.claim_id)["dispute"] == 1
    err("owned", claims.approve, conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW, transfer=True)
    assert company(conn, cid)["owner_user_id"] == rival["id"]


def test_own_company_cannot_be_claimed_again(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    err("own", submit_claim, conn, cid, boss["id"])


# ------------------------------------------------------------------ решения

def test_approve_sets_owner_dates_and_moves_vacancies(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    res = claims.approve(conn, out.claim_id, ADMIN, "позвонили", "127.0.0.1", SITE, NOW)
    assert res.status == "confirmed" and res.mail.to == "boss@example.test"
    c = company(conn, cid)
    assert c["owner_user_id"] == boss["id"] and c["claimed_at"] and c["verified_at"] and c["origin"] == "auto"
    owners = {r[0] for r in rows(conn, "SELECT owner_user_id FROM vacancies WHERE company_id=?", cid)}
    assert owners == {boss["id"]}
    k = claims.get(conn, out.claim_id)
    assert k["status"] == "confirmed" and k["decided_by"] == ADMIN["id"] and k["admin_note"] == "позвонили"
    assert [r[0] for r in rows(conn, "SELECT action FROM audit_log WHERE target_type='claim' ORDER BY id")] == ["claim.submit", "claim.approve"]


def test_approve_requires_review_state(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"], method="email", contact="boss@zerno.example")
    err("state", claims.approve, conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)


def test_approve_rejects_other_open_claims_and_mails_them(conn, cid, boss, rival):
    first = submit_claim(conn, cid, boss["id"])
    second = submit_claim(conn, cid, rival["id"])
    res = claims.approve(conn, first.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    assert claims.get(conn, second.claim_id)["status"] == "rejected"
    assert {m.to for m in res.mails} == {"boss@example.test", "rival@example.test"}


def test_one_owner_per_company_no_second_approve(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    err("state", claims.approve, conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)


def test_approve_merges_into_applicants_own_company(conn, cid, boss):
    conn.execute("INSERT INTO companies(owner_user_id, name, city, origin) VALUES (?, 'Моя кофейня', 'Донецк', 'registered')", (boss["id"],))
    conn.commit()
    mine = rows(conn, "SELECT id FROM companies WHERE owner_user_id=?", boss["id"])[0][0]
    out = submit_claim(conn, cid, boss["id"])
    res = claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    assert res.detail["merged_into"] == mine
    assert company(conn, cid)["merged_into"] == mine
    assert {r[0] for r in rows(conn, "SELECT company_id FROM vacancies WHERE employer='Кофейня Зерно'")} == {mine}
    assert company(conn, mine)["verified_at"]
    assert len(rows(conn, "SELECT id FROM companies WHERE owner_user_id=?", boss["id"])) == 1


def test_reject_needs_reason_and_blocks_for_a_week(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    err("reason", claims.reject, conn, out.claim_id, ADMIN, "  ", "127.0.0.1", SITE, NOW)
    res = claims.reject(conn, out.claim_id, ADMIN, "не подтвердили", "127.0.0.1", SITE, NOW)
    assert res.status == "rejected" and "не подтвердили" in res.mail.body
    err("cooldown", submit_claim, conn, cid, boss["id"], now=NOW + timedelta(days=3))
    again = submit_claim(conn, cid, boss["id"], now=NOW + timedelta(days=8))
    assert again.status == "review"
    assert company(conn, cid)["owner_user_id"] is None


def test_request_docs_keeps_claim_open_and_note_appends(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    res = claims.request_docs(conn, out.claim_id, ADMIN, "Пришлите ИНН", "127.0.0.1", SITE, NOW)
    assert res.mail is not None and "Пришлите ИНН" in res.mail.body
    k = claims.get(conn, out.claim_id)
    assert k["status"] == "review" and k["docs_note"] == "Пришлите ИНН"
    claims.add_note(conn, out.claim_id, boss["id"], "ИНН 9300000001", NOW)
    assert "ИНН 9300000001" in claims.get(conn, out.claim_id)["message"]
    err("empty", claims.add_note, conn, out.claim_id, boss["id"], " ", NOW)


def test_withdraw(conn, cid, boss, rival):
    out = submit_claim(conn, cid, boss["id"])
    assert claims.withdraw(conn, out.claim_id, rival["id"], NOW) is False, "чужую заявку отозвать нельзя"
    assert claims.withdraw(conn, out.claim_id, boss["id"], NOW) is True
    assert claims.get(conn, out.claim_id)["status"] == "rejected"
    err("none", claims.add_note, conn, out.claim_id, boss["id"], "ещё", NOW)


def test_release_owner_keeps_auto_card_and_vacancies(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    assert claims.release_owner(conn, boss["id"]) == 1
    conn.commit()
    c = company(conn, cid)
    assert c["owner_user_id"] is None and c["verified_at"] is None and c["claimed_at"] is None
    assert len(rows(conn, "SELECT id FROM vacancies WHERE company_id=? AND owner_user_id IS NULL", cid)) == 2


def test_deleting_account_does_not_delete_collected_vacancies(conn, cid, boss):
    out = submit_claim(conn, cid, boss["id"])
    claims.approve(conn, out.claim_id, ADMIN, "", "127.0.0.1", SITE, NOW)
    service.delete_account(conn, boss["id"])
    assert len(rows(conn, "SELECT id FROM vacancies WHERE company_id=?", cid)) == 2
    assert company(conn, cid)["owner_user_id"] is None
    assert rows(conn, "SELECT COUNT(*) FROM company_claims")[0][0] == 0
