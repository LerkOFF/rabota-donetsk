"""Доступ в админку: роль, noindex, CSRF на всех POST, лимит попыток."""
import re

import pytest

from tests.accounts.harness import PASSWORD, login, make_user
from tests.web.admin_harness import add_vacancy, apost, login_admin, make_env, meta


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


GET_PAGES = ["/admin", "/admin/moderation", "/admin/complaints", "/admin/reports", "/admin/stoplist",
             "/admin/requests", "/admin/audit"]


@pytest.mark.parametrize("path", GET_PAGES)
def test_guest_redirected_to_login(env, path):
    r = env.client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/admin")


@pytest.mark.parametrize("role", ["job_seeker", "employer"])
def test_other_roles_get_404(env, role):
    make_user(env, f"{role}@example.test", role=role)
    assert login(env, f"{role}@example.test", PASSWORD).status_code == 303
    for path in GET_PAGES:
        assert env.client.get(path).status_code == 404


def test_denied_attempts_are_limited(env):
    make_user(env, "seeker@example.test")
    login(env, "seeker@example.test", PASSWORD)
    env.app.state.admin.limiter.limits["admin_denied"] = (3, 900)
    codes = [env.client.get("/admin").status_code for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]


def test_admin_sees_dashboard_with_noindex(env):
    login_admin(env)
    r = env.client.get("/admin")
    assert r.status_code == 200
    assert "noindex" in r.headers["x-robots-tag"] and "nofollow" in r.headers["x-robots-tag"]
    assert "noindex" in meta(r.text, "robots") and "nofollow" in meta(r.text, "robots")
    assert "<title>" in r.text and meta(r.text, "description") and 'rel="canonical"' in r.text
    assert "no-store" in r.headers["cache-control"]


@pytest.mark.parametrize("path", GET_PAGES)
def test_every_admin_page_has_seo_block(env, path):
    login_admin(env)
    r = env.client.get(path)
    assert r.status_code == 200
    assert "noindex" in meta(r.text, "robots")
    assert 'rel="canonical" href="https://example.test' + path in r.text
    assert 40 < len(meta(r.text, "description")) < 200


def test_blocked_admin_loses_access(env):
    uid = login_admin(env)
    conn = env.db()
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    r = env.client.get("/admin", follow_redirects=False)
    assert r.status_code == 303


def test_robots_txt_closes_admin(env):
    assert "Disallow: /admin" in env.client.get("/robots.txt").text


def test_public_request_form_noindex_follow(env):
    r = env.client.get("/legal/request")
    assert r.status_code == 200
    assert meta(r.text, "robots") == "noindex, follow"
    assert "noindex" in r.headers["x-robots-tag"]


# ------------------------------------------------------------------ CSRF на каждом POST

def post_targets(env):
    vid = add_vacancy(env, moderation_status="pending", status="hidden")
    conn = env.db()
    conn.execute("INSERT INTO complaints(vacancy_id, reason) VALUES (?, 'Другое: тест')", (vid,))
    conn.execute("INSERT INTO reports(target_type, target_id, reason) VALUES ('user', 1, 'x')")
    conn.execute("INSERT INTO stoplist(kind, value, normalized, created_at) VALUES ('keyword','x','xxx','2026-01-01')")
    conn.execute("INSERT INTO legal_requests(created_at, kind, due_at) VALUES ('2026-10-29T12:00:00+00:00','access','2026-11-12T12:00:00+00:00')")
    conn.commit()
    conn.close()
    return [
        f"/admin/vacancies/{vid}/approve", f"/admin/vacancies/{vid}/reject", f"/admin/vacancies/{vid}/hide",
        f"/admin/vacancies/{vid}/restore", f"/admin/vacancies/{vid}/edit",
        "/admin/complaints/1/hide", "/admin/complaints/1/stoplist", "/admin/complaints/1/dismiss",
        "/admin/complaints/1/reply", "/admin/reports/1/dismiss", "/admin/reports/1/resolve", "/admin/reports/1/block",
        "/admin/stoplist/add", "/admin/stoplist/1/delete", "/admin/requests/new", "/admin/requests/1/update",
        "/admin/requests/1/erase",
    ]


def test_all_posts_require_csrf_and_origin(env):
    login_admin(env)
    targets = post_targets(env)
    for path in targets:
        r = env.client.post(path, data={}, follow_redirects=False)
        assert r.status_code == 403, path
        token_page = env.client.get("/admin").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_page).group(1)
        r = env.client.post(path, data={"csrf_token": token}, headers={"origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403, path
        r = env.client.post(path, data={"csrf_token": "0" * 64}, follow_redirects=False)
        assert r.status_code == 403, path


def test_posts_require_admin_role(env):
    make_user(env, "seeker@example.test")
    login(env, "seeker@example.test", PASSWORD)
    r = apost(env, "/admin/stoplist/add", {"kind": "keyword", "value": "тест"}, page="/account")
    assert r.status_code == 404
    assert env.query("SELECT COUNT(*) c FROM stoplist")[0]["c"] == 0


def test_guest_post_redirects_to_login(env):
    r = env.client.post("/admin/stoplist/add", data={"kind": "keyword", "value": "тест"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_post_rate_limit_per_admin(env):
    login_admin(env)
    env.app.state.admin.limiter.limits["admin_post"] = (2, 60)
    codes = [apost(env, "/admin/stoplist/add", {"kind": "keyword", "value": f"слово{i}"}).status_code for i in range(4)]
    assert codes[:2] == [303, 303] and codes[2:] == [429, 429]
