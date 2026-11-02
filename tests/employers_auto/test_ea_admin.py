"""Админка: заявки на карточки, решение, слияние дублей, пустые автокарточки, доступ, CSRF, журнал."""
from __future__ import annotations

import re

import pytest

from rabota.employers_auto import merge
from tests.accounts.harness import PASSWORD, build_env, login, make_user
from tests.employers_auto.kit import NOW, auto_company, submit_claim
from tests.web.admin_harness import apost, login_admin

PHONE = "+79490001122"


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def setup(env):
    """Автокарточка, работодатель и его заявка на проверке."""
    uid = make_user(env, "boss@example.test", role="employer", name="Олег")
    conn = env.db()
    try:
        cid = auto_company(conn, "Кофейня Зерно", count=3, contacts={"phones": [PHONE]})
        out = submit_claim(conn, cid, uid, method="phone", contact=PHONE, message="Я директор")
    finally:
        conn.close()
    return cid, uid, out.claim_id


def rows(env, sql, *a):
    return env.query(sql, *a)


def text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


PATHS = ["/admin/claims", "/admin/claims/1", "/admin/claims/duplicates", "/admin/claims/empty"]


# ------------------------------------------------------------------ доступ

def test_guest_goes_to_login(env, setup):
    for p in PATHS:
        r = env.client.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/admin")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_other_roles_get_404(env, setup, role):
    make_user(env, f"{role}2@example.test", role=role)
    assert login(env, f"{role}2@example.test", PASSWORD).status_code == 303
    for p in PATHS:
        assert env.client.get(p).status_code == 404
    for p in ("/admin/claims/1/approve", "/admin/claims/1/reject", "/admin/claims/1/docs", "/admin/claims/merge", "/admin/claims/empty/delete"):
        assert env.client.post(p, data={}).status_code == 404


def test_posts_require_csrf(env, setup):
    login_admin(env)
    for action in ("approve", "reject", "docs"):
        assert env.client.post(f"/admin/claims/1/{action}", data={"note": "x", "reason": "x", "docs": "x"}).status_code == 403
    assert rows(env, "SELECT status FROM company_claims")[0][0] == "review"
    assert not rows(env, "SELECT 1 FROM audit_log WHERE action IN ('claim.approve','claim.reject','claim.docs')")


# ------------------------------------------------------------------ список и карточка

def test_menu_and_counter(env, setup):
    login_admin(env)
    page = env.client.get("/admin")
    assert "/admin/claims" in page.text and "Заявки на карточки" in page.text


def test_list_filters_and_empty_states(env, setup):
    login_admin(env)
    page = env.client.get("/admin/claims")
    assert page.status_code == 200 and "Кофейня Зерно" in page.text and "boss@example.test" in page.text
    assert 'data-sortable="claims-tiles"' in page.text and 'data-key="review"' in page.text and 'data-sortable="claims-columns"' in page.text
    assert "Кофейня Зерно" in env.client.get("/admin/claims?q=boss").text
    nothing = env.client.get("/admin/claims?q=несуществующее")
    assert "Ничего не нашлось" in nothing.text and "Сбросить фильтры" in nothing.text
    assert "Кофейня Зерно" not in env.client.get("/admin/claims?view=confirmed").text
    assert env.client.get("/admin/claims?view=zzz&method=zzz&period=zzz&sort=zzz&page=abc").status_code == 200


def test_list_empty_without_claims(env):
    login_admin(env)
    assert "Заявок нет" in env.client.get("/admin/claims").text


def test_claim_card_shows_facts(env, setup):
    _, _, kid = setup
    login_admin(env)
    page = env.client.get(f"/admin/claims/{kid}")
    t = text(page.text)
    assert page.status_code == 200
    assert "Кофейня Зерно" in t and "boss@example.test" in t and "Я директор" in t
    assert "Телефон есть в вакансиях" in t and "Подтвердить заявку" in t and "Отклонить заявку" in t
    assert 'data-sortable="claim-main"' in page.text
    assert env.client.get("/admin/claims/9999").status_code == 404


# ------------------------------------------------------------------ решения

def test_approve_flow(env, setup):
    cid, uid, kid = setup
    login_admin(env)
    r = apost(env, f"/admin/claims/{kid}/approve", {"note": "позвонили"}, page=f"/admin/claims/{kid}")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/claims/{kid}")
    c = rows(env, "SELECT * FROM companies WHERE id=?", cid)[0]
    assert c["owner_user_id"] == uid and c["claimed_at"] and c["verified_at"] and c["origin"] == "auto"
    assert rows(env, "SELECT status, admin_note FROM company_claims")[0][:] == ("confirmed", "позвонили")
    assert {r["owner_user_id"] for r in rows(env, "SELECT owner_user_id FROM vacancies WHERE company_id=?", cid)} == {uid}
    audit = rows(env, "SELECT action, actor_email FROM audit_log WHERE action='claim.approve'")
    assert audit and audit[0]["actor_email"] == "admin@example.test"
    assert any(m.to == "boss@example.test" and "теперь ваша" in m.subject for m in env.mailer.outbox)
    # вакансии появились в кабинете владельца
    env.client.post("/logout", data={}, follow_redirects=False)
    assert login(env, "boss@example.test", PASSWORD).status_code == 303
    assert "бариста" in env.client.get("/cabinet/vacancies").text.lower()


def test_approve_twice_shows_error(env, setup):
    _, _, kid = setup
    login_admin(env)
    apost(env, f"/admin/claims/{kid}/approve", {}, page=f"/admin/claims/{kid}")
    again = apost(env, f"/admin/claims/{kid}/approve", {}, page=f"/admin/claims/{kid}")
    assert again.status_code == 422 and "закрыта" in again.text


def test_reject_needs_reason(env, setup):
    cid, _, kid = setup
    login_admin(env)
    bad = apost(env, f"/admin/claims/{kid}/reject", {"reason": " "}, page=f"/admin/claims/{kid}")
    assert bad.status_code == 422 and "причину" in bad.text
    ok = apost(env, f"/admin/claims/{kid}/reject", {"reason": "нет подтверждения"}, page=f"/admin/claims/{kid}")
    assert ok.status_code == 303
    assert rows(env, "SELECT status FROM company_claims")[0][0] == "rejected"
    assert rows(env, "SELECT owner_user_id FROM companies WHERE id=?", cid)[0][0] is None
    assert any("нет подтверждения" in m.body for m in env.mailer.outbox)
    assert rows(env, "SELECT 1 FROM audit_log WHERE action='claim.reject'")


def test_request_docs(env, setup):
    _, _, kid = setup
    login_admin(env)
    r = apost(env, f"/admin/claims/{kid}/docs", {"docs": "Пришлите ИНН"}, page=f"/admin/claims/{kid}")
    assert r.status_code == 303
    assert rows(env, "SELECT docs_note, status FROM company_claims")[0][:] == ("Пришлите ИНН", "review")
    assert "Пришлите ИНН" in text(env.client.get(f"/admin/claims/{kid}").text)
    assert apost(env, f"/admin/claims/{kid}/docs", {"docs": ""}, page=f"/admin/claims/{kid}").status_code == 422


def test_dispute_is_highlighted_and_transfer_flag(env, setup):
    cid, _, _ = setup
    rival = make_user(env, "rival@example.test", role="employer", name="Игорь")
    conn = env.db()
    second = submit_claim(conn, cid, rival, method="phone", contact=PHONE).claim_id
    conn.close()
    login_admin(env)
    page = env.client.get(f"/admin/claims/{second}")
    assert "Спор" in page.text and "Другие заявки на эту компанию" in page.text
    assert "Спор" in env.client.get("/admin/claims?view=dispute").text


# ------------------------------------------------------------------ слияние и пустые карточки

def test_duplicates_page_and_merge(env, setup):
    _, _, _ = setup
    conn = env.db()
    dup = auto_company(conn, "Кофейня Зёрна", count=1, source="@other")
    conn.close()
    login_admin(env)
    page = env.client.get("/admin/claims/duplicates")
    assert "Кофейня Зерно" in page.text and "Влить" in page.text and "csrf_token" in page.text
    pair = merge_pair(env)
    main_id, dup_id = pair
    r = apost(env, "/admin/claims/merge", {"main": str(main_id), "dup": str(dup_id)}, page="/admin/claims/duplicates")
    assert r.status_code == 303
    assert rows(env, "SELECT merged_into FROM companies WHERE id=?", dup_id)[0][0] == main_id
    assert not rows(env, "SELECT 1 FROM vacancies WHERE company_id=?", dup_id)
    assert rows(env, "SELECT 1 FROM audit_log WHERE action='company.merge'")
    redirect = env.client.get(f"/company/{dup_id}", follow_redirects=False)
    assert redirect.status_code == 301 and redirect.headers["location"] == f"/company/{main_id}"
    assert dup in (main_id, dup_id)


def merge_pair(env) -> tuple[int, int]:
    from rabota.db import connect

    conn = connect(env.db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        p = merge.find_duplicates(conn)[0]
        return p["main"]["id"], p["dup"]["id"]
    finally:
        conn.close()


def test_merge_validation_errors(env, setup):
    cid, _, _ = setup
    login_admin(env)
    same = apost(env, "/admin/claims/merge", {"main": str(cid), "dup": str(cid)}, page="/admin/claims/duplicates")
    assert same.status_code == 422 and "саму с собой" in same.text
    assert apost(env, "/admin/claims/merge", {"main": "x", "dup": "y"}, page="/admin/claims/duplicates").status_code == 400
    assert apost(env, "/admin/claims/merge", {"main": str(cid), "dup": "9999"}, page="/admin/claims/duplicates").status_code == 422


def test_merge_service_moves_everything_and_keeps_owner(env, setup):
    cid, _, _ = setup
    conn = env.db()
    try:
        dup = auto_company(conn, "Кофейня Зёрна", count=2, source="@o2")
        res = merge.merge_companies(conn, cid, dup, {"id": 1, "email": "a@example.test", "role": "admin"}, "127.0.0.1", NOW)
        assert res["moved"] == 2
        assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE company_id=?", (cid,)).fetchone()[0] == 5
        with pytest.raises(merge.MergeError):
            merge.merge_companies(conn, cid, dup, None)
        assert merge.find_duplicates(conn) == [] or all(p["dup"]["id"] != dup for p in merge.find_duplicates(conn))
    finally:
        conn.close()


def test_empty_companies_page_and_delete(env, setup):
    cid, _, _ = setup
    conn = env.db()
    empty = conn.execute("INSERT INTO companies(owner_user_id, name, origin, norm_name, city) VALUES (NULL,'Пустая лавка','auto','пустая лавка','Донецк')").lastrowid
    conn.commit()
    conn.close()
    login_admin(env)
    page = env.client.get("/admin/claims/empty")
    assert "Пустая лавка" in page.text and 'data-sortable="empty-columns"' in page.text
    assert "Кофейня Зерно" not in page.text
    assert "Кофейня Зерно" not in env.client.get("/admin/claims/empty?view=nolive").text
    conn = env.db()
    conn.execute("UPDATE vacancies SET status='archived' WHERE company_id=?", (cid,))
    conn.commit()
    conn.close()
    nolive = env.client.get("/admin/claims/empty?view=nolive").text
    assert "Кофейня Зерно" in nolive and "в архиве: 3" in text(nolive)
    r = apost(env, "/admin/claims/empty/delete", {"ids": [str(empty), str(cid)]}, page="/admin/claims/empty")
    assert r.status_code == 303
    assert not rows(env, "SELECT 1 FROM companies WHERE id=?", empty)
    assert rows(env, "SELECT 1 FROM companies WHERE id=?", cid), "карточку с вакансиями удалить нельзя"
    assert rows(env, "SELECT 1 FROM audit_log WHERE action='company.delete_empty'")
    assert env.client.get("/admin/claims/empty?view=zzz&page=abc").status_code == 200
