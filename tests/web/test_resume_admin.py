"""Раздел «Резюме» админки: список, фильтры, карточка, скрытие и возврат, журнал, права, CSRF."""
import pytest

from rabota.resumes import rules, service
from tests.accounts.harness import build_env, login_as
from tests.resumes.conftest import NOW
from tests.web.admin_harness import apost, login_admin, one, rows
from tests.web.resume_kit import FORM, form


@pytest.fixture
def env(tmp_path):
    e = build_env(tmp_path)
    with e.client:
        yield e


def add_resume(env, owner_email="maria@example.test", **over) -> int:
    from tests.accounts.harness import make_user

    uid = env.user(owner_email)["id"] if env.user(owner_email) else make_user(env, owner_email, name="Мария Петрова")
    clean, errors = rules.validate(type("F", (dict,), {"getlist": lambda self, k: self.get(k, []) if isinstance(self.get(k, []), list) else [self[k]]})(form(**over)))
    assert not errors, errors
    conn = env.db()
    try:
        return service.create(conn, uid, clean, NOW)
    finally:
        conn.close()


def test_admin_menu_has_resumes_and_page_opens(env):
    login_admin(env)
    add_resume(env)
    page = env.client.get("/admin")
    assert 'href="/admin/resumes"' in page.text and "Резюме" in page.text
    r = env.client.get("/admin/resumes")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"] and r.headers["cache-control"] == "no-store"
    for needle in ('data-sortable="resumes-tiles"', 'data-sortable="resumes-columns"', 'data-key="all"', 'data-key="public"',
                   'data-key="id"', "Бариста", "maria@example.test", "Всем работодателям", "Найдено: 1"):
        assert needle in r.text, needle


def test_counts_and_filters(env):
    login_admin(env)
    a = add_resume(env)
    add_resume(env, visibility="open")
    add_resume(env, visibility="hidden", consent=None)
    env.client.get("/admin")
    conn = env.db()
    conn.execute("UPDATE resumes SET admin_hidden=1 WHERE id=?", (a,))
    conn.execute("UPDATE resumes SET updated_at='2024-01-01T00:00:00+00:00' WHERE id=3")
    conn.commit()
    conn.close()
    get = lambda q: env.client.get("/admin/resumes?" + q).text
    assert "Найдено: 3" in get("")
    assert "Найдено: 1" in get("view=open") and "Найдено: 1" in get("view=hidden")
    assert "Найдено: 1" in get("view=admin_hidden") and "Найдено: 1" in get("view=stale")
    assert "Найдено: 1" in get("q=%D0%BA%D0%BE%D1%84%D0%B5") or "Найдено: 3" in get("q=кофе")
    assert "Найдено: 1" in get(f"q={a}") and "Найдено: 0" in get("q=zzzz")
    assert "Найдено: 3" in get("view=bad&page=zzz")


def test_card_shows_consent_and_stats(env):
    login_admin(env)
    rid = add_resume(env)
    r = env.client.get(f"/admin/resumes/{rid}")
    assert r.status_code == 200
    for needle in ("Резюме №1: Бариста", "maria@example.test", "Согласие на передачу", "resume 02.11.2026", "100%", "+7 949 111-22-33",
                   'data-sortable="resume-blocks"', 'data-key="moderation"', f'action="/admin/resumes/{rid}/hide"'):
        assert needle in r.text, needle
    assert env.client.get("/admin/resumes/999").status_code == 404


def test_hide_requires_reason_csrf_and_is_logged(env):
    admin_id = login_admin(env)
    rid = add_resume(env)
    page = f"/admin/resumes/{rid}"
    r = apost(env, f"{page}/hide", {"note": "  "}, page=page)
    assert r.status_code == 422 and "Напишите причину" in r.text
    assert one(env, "SELECT admin_hidden FROM resumes")["admin_hidden"] == 0
    assert env.client.post(f"{page}/hide", data={"note": "x"}, headers={"origin": "http://testserver"}).status_code == 403
    r = apost(env, f"{page}/hide", {"note": "Чужие данные в тексте", "next": page}, page=page)
    assert r.status_code == 303 and "ok=resume_hidden" in r.headers["location"]
    saved = one(env, "SELECT * FROM resumes")
    assert saved["admin_hidden"] == 1 and saved["admin_note"] == "Чужие данные в тексте" and saved["admin_hidden_at"]
    log = rows(env, "SELECT * FROM audit_log WHERE action='resume.hide'")
    assert len(log) == 1 and log[0]["actor_user_id"] == admin_id and log[0]["target_id"] == str(rid) and "Чужие данные" in log[0]["details"]
    assert "Резюме скрыто" in env.client.get(r.headers["location"]).text


def test_restore_returns_resume_and_is_logged(env):
    login_admin(env)
    rid = add_resume(env)
    page = f"/admin/resumes/{rid}"
    apost(env, f"{page}/hide", {"note": "причина"}, page=page)
    assert "Вернуть владельцу" in env.client.get(page).text
    r = apost(env, f"{page}/restore", {}, page=page)
    assert r.status_code == 303 and "ok=resume_restored" in r.headers["location"]
    assert one(env, "SELECT admin_hidden, admin_note FROM resumes")["admin_hidden"] == 0
    assert len(rows(env, "SELECT * FROM audit_log WHERE action='resume.restore'")) == 1
    assert apost(env, f"{page}/restore", {}, page=page).status_code == 404  # возвращать нечего


def test_hidden_resume_leaves_employer_search(env):
    from tests.messaging.webkit import second_visitor

    login_admin(env)
    rid = add_resume(env)
    boss = second_visitor(env)
    login_as(boss, "boss@example.test", role="employer")
    assert "Найдено: 1" in boss.client.get("/employers/resumes").text
    page = f"/admin/resumes/{rid}"
    apost(env, f"{page}/hide", {"note": "причина"}, page=page)
    assert "Найдено: 0" in boss.client.get("/employers/resumes").text
    assert boss.client.get(f"/employers/resumes/{rid}").status_code == 404


def test_only_admin_reaches_the_section(env):
    rid = add_resume(env)
    assert env.client.get("/admin/resumes", follow_redirects=False).status_code == 303  # гость: на вход
    login_as(env, "boss@example.test", role="employer")
    for path in ("/admin/resumes", f"/admin/resumes/{rid}"):
        assert env.client.get(path).status_code == 404, path
    r = env.client.post(f"/admin/resumes/{rid}/hide", data={"note": "x"})
    assert r.status_code in (403, 404)
    assert one(env, "SELECT admin_hidden FROM resumes")["admin_hidden"] == 0
    assert FORM  # данные анкеты общие с другими тестами
