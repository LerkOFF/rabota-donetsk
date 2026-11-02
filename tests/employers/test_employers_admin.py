"""Раздел админки «Работодатели»: список, фильтры, карточка, проверка и блокировка с CSRF и журналом."""
from __future__ import annotations

import pytest

from tests.accounts.harness import PASSWORD, login, make_user
from tests.cabinet.conftest import make_row
from tests.web.admin_harness import apost, login_admin, make_env, meta, one, rows


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


@pytest.fixture
def admin(env):
    return login_admin(env)


def company(env, name="Кофейня Зерно", email="boss@example.test", **cols) -> tuple[int, int]:
    uid = make_user(env, email, role="employer", name="Олег")
    conn = env.db()
    fields = {"owner_user_id": uid, "name": name, "city": "Донецк", "site": "https://zerno.example", **cols}
    cur = conn.execute(f"INSERT INTO companies({','.join(fields)}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
    conn.commit()
    conn.close()
    return uid, int(cur.lastrowid)


def vacancy(env, uid, cid, **kw) -> int:
    conn = env.db()
    try:
        return make_row(conn, uid, company_id=cid, **kw)
    finally:
        conn.close()


def audit_actions(env) -> list[str]:
    return [r["action"] for r in rows(env, "SELECT action FROM audit_log ORDER BY id")]


# ------------------------------------------------------------------ доступ

@pytest.mark.parametrize("path", ["/admin/employers", "/admin/employers/1"])
def test_guest_is_redirected_to_login(env, path):
    r = env.client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/admin")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_other_roles_get_404(env, role):
    make_user(env, f"{role}@example.test", role=role)
    login(env, f"{role}@example.test", PASSWORD)
    assert env.client.get("/admin/employers").status_code == 404
    assert env.client.get("/admin/employers/1").status_code == 404
    assert env.client.post("/admin/employers/1/verify", data={}).status_code in (403, 404)


def test_pages_have_noindex_and_seo(env, admin):
    _, cid = company(env)
    for path in ("/admin/employers", f"/admin/employers/{cid}"):
        r = env.client.get(path)
        assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"] and "no-store" in r.headers["cache-control"]
        assert "noindex" in meta(r.text, "robots") and f'rel="canonical" href="https://example.test{path}"' in r.text
        assert 40 < len(meta(r.text, "description")) < 200 and r.text.count("<h1") == 1


def test_menu_item_exists(env, admin):
    assert 'href="/admin/employers"' in env.client.get("/admin").text


# ------------------------------------------------------------------ список

def test_list_shows_companies_and_counts(env, admin):
    uid, cid = company(env)
    vacancy(env, uid, cid)
    vacancy(env, uid, cid, profession="кассир", moderation="pending", status="hidden")
    html = env.client.get("/admin/employers").text
    assert "Кофейня Зерно" in html and f"/admin/employers/{cid}" in html and "boss@example.test" in html


def test_list_empty_state(env, admin):
    html = env.client.get("/admin/employers").text
    assert "Компаний" in html or "нет" in html.lower()


def test_list_has_sortable_blocks(env, admin):
    company(env)
    html = env.client.get("/admin/employers").text
    assert 'data-sortable="employers-tiles"' in html and 'data-sortable="employers-columns"' in html
    assert 'data-key="' in html and 'draggable="true"' in html


def test_filters_search_verified_state(env, admin):
    company(env, "Кофейня Зерно", "a@example.test", verified_at="2026-10-01T10:00:00+00:00")
    company(env, "Склад Плюс", "b@example.test")
    company(env, "Блок Групп", "c@example.test", blocked_at="2026-10-02T10:00:00+00:00")

    def names(query):
        html = env.client.get("/admin/employers" + query).text
        return {n for n in ("Кофейня Зерно", "Склад Плюс", "Блок Групп") if f">{n}<" in html or f"{n}</a>" in html}

    assert names("") == {"Кофейня Зерно", "Склад Плюс", "Блок Групп"}
    assert names("?verified=yes") == {"Кофейня Зерно"}
    assert names("?verified=no") == {"Склад Плюс", "Блок Групп"}
    assert names("?state=blocked") == {"Блок Групп"}
    assert names("?state=active") == {"Кофейня Зерно", "Склад Плюс"}
    assert names("?q=склад") == {"Склад Плюс"}
    assert names("?q=b@example") == {"Склад Плюс"}
    assert names("?q=zzzz") == set()


def test_bad_filters_are_ignored(env, admin):
    company(env)
    r = env.client.get("/admin/employers?verified=x&state=y&period=z&sort=drop&dir=up&page=-4&q=" + "я" * 500)
    assert r.status_code == 200


def test_sql_in_search_is_harmless(env, admin):
    company(env)
    r = env.client.get("/admin/employers?q=" + "'; DROP TABLE companies;--")
    assert r.status_code == 200 and "Кофейня Зерно" not in r.text
    assert one(env, "SELECT COUNT(*) AS n FROM companies")["n"] == 1


# ------------------------------------------------------------------ карточка и действия

def test_card_shows_data_vacancies_flags_and_history(env, admin):
    uid, cid = company(env)
    vid = vacancy(env, uid, cid, moderation="pending", status="hidden")
    conn = env.db()
    conn.execute("UPDATE vacancies SET automod_flags = '[\"money\"]' WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    html = env.client.get(f"/admin/employers/{cid}").text
    assert "Кофейня Зерно" in html and "zerno.example" in html and f"/admin/vacancies/{vid}" in html
    assert "Просьба заплатить" in html  # пометка автопроверки видна администратору
    assert 'data-sortable="' in html


def test_unknown_company_404(env, admin):
    assert env.client.get("/admin/employers/999").status_code == 404
    assert apost(env, "/admin/employers/999/verify", page="/admin/employers").status_code == 404


def test_verify_and_unverify_with_audit(env, admin):
    _, cid = company(env)
    r = apost(env, f"/admin/employers/{cid}/verify", {"note": "звонили по телефону"}, page="/admin/employers")
    assert r.status_code == 303
    row = one(env, "SELECT verified, verified_at, verified_by, verify_note FROM companies WHERE id=?", cid)
    assert row["verified"] == 1 and row["verified_at"] and row["verified_by"] == admin and row["verify_note"] == "звонили по телефону"
    assert "Работодатель отмечен как проверенный" in env.client.get(r.headers["location"]).text
    assert "Проверенный работодатель" in env.client.get(f"/company/{cid}").text
    assert apost(env, f"/admin/employers/{cid}/verify", page="/admin/employers").status_code == 303  # повтор безвреден
    r = apost(env, f"/admin/employers/{cid}/unverify", page="/admin/employers")
    assert r.status_code == 303
    row = one(env, "SELECT verified, verified_at FROM companies WHERE id=?", cid)
    assert row["verified"] == 0 and row["verified_at"] is None
    assert "Проверенный работодатель" not in env.client.get(f"/company/{cid}").text
    acts = audit_actions(env)
    assert acts.count("employer.verify") == 1 and "employer.unverify" in acts


def test_block_requires_reason(env, admin):
    _, cid = company(env)
    r = apost(env, f"/admin/employers/{cid}/block", {"reason": "  "}, page="/admin/employers")
    assert r.status_code == 422 and "Укажите причину" in r.text
    assert one(env, "SELECT blocked_at FROM companies WHERE id=?", cid)["blocked_at"] is None


def test_block_hides_everything_and_unblock_restores_only_its_own(env, admin):
    uid, cid = company(env, verified_at="2026-10-01T10:00:00+00:00")
    live = vacancy(env, uid, cid)
    pending = vacancy(env, uid, cid, profession="кассир", moderation="pending", status="hidden")
    by_moderator = vacancy(env, uid, cid, profession="повар", status="hidden", note="Скрыто модератором")
    assert env.client.get(f"/vacancy/{live}").status_code == 200

    r = apost(env, f"/admin/employers/{cid}/block", {"reason": "обман соискателей"}, page="/admin/employers")
    assert r.status_code == 303
    c = one(env, "SELECT * FROM companies WHERE id=?", cid)
    assert c["blocked_at"] and c["blocked_note"] == "обман соискателей" and c["verified_at"] is None
    assert one(env, "SELECT status FROM users WHERE id=?", uid)["status"] == "blocked"
    assert env.client.get(f"/vacancy/{live}").status_code == 404
    assert env.client.get(f"/company/{cid}").status_code == 404
    assert "Работодатель заблокирован" in one(env, "SELECT moderation_note FROM vacancies WHERE id=?", live)["moderation_note"]

    assert apost(env, f"/admin/employers/{cid}/verify", page="/admin/employers").status_code == 422  # заблокированного не проверить
    r = apost(env, f"/admin/employers/{cid}/unblock", page="/admin/employers")
    assert r.status_code == 303
    assert one(env, "SELECT status FROM users WHERE id=?", uid)["status"] != "blocked"
    assert env.client.get(f"/vacancy/{live}").status_code == 200
    assert one(env, "SELECT status FROM vacancies WHERE id=?", by_moderator)["status"] == "hidden"
    assert one(env, "SELECT moderation_status FROM vacancies WHERE id=?", pending)["moderation_status"] == "pending"
    acts = audit_actions(env)
    assert "employer.block" in acts and "employer.unblock" in acts


def test_blocked_owner_cannot_post_vacancy(env, admin):
    _, cid = company(env)
    apost(env, f"/admin/employers/{cid}/block", {"reason": "обман"}, page="/admin/employers")
    from fastapi.testclient import TestClient

    from tests.web.postkit import Poster

    with TestClient(env.app, base_url="http://testserver") as guest:
        pk = Poster(guest, env.mailer)
        r = pk.confirm(pk.token_of(pk.submit({
            "profession": "Бариста", "phone": "+7 949 000-00-77", "summary": "Готовить кофе и общаться с гостями."})), email="boss@example.test")
    assert r.status_code == 403


@pytest.mark.parametrize("action", ["verify", "unverify", "block", "unblock"])
def test_actions_need_csrf(env, admin, action):
    _, cid = company(env)
    r = env.client.post(f"/admin/employers/{cid}/{action}", data={"reason": "x"}, follow_redirects=False)
    assert r.status_code == 403
    r = env.client.post(f"/admin/employers/{cid}/{action}", data={"csrf_token": "bad", "reason": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert audit_actions(env) == []


def test_actions_are_post_only(env, admin):
    _, cid = company(env)
    assert env.client.get(f"/admin/employers/{cid}/verify").status_code == 405


def test_automod_flags_visible_in_vacancy_admin_card(env, admin):
    uid, cid = company(env)
    vid = vacancy(env, uid, cid, moderation="pending", status="hidden")
    conn = env.db()
    conn.execute("UPDATE vacancies SET automod_flags = '[\"links\", \"caps\"]' WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    html = env.client.get(f"/admin/vacancies/{vid}").text
    assert "Ссылки или приглашение" in html and f"/admin/employers/{cid}" in html
    assert "Автопроверка" in env.client.get("/admin/moderation").text
