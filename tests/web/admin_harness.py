"""Помощники тестов админки поверх обвязки тестов аккаунтов."""
from __future__ import annotations

import re
from pathlib import Path

from rabota.db import connect, save_vacancy
from rabota.models import Vacancy
from tests.accounts.harness import PASSWORD, Env, build_env, csrf_of, login, make_user

ADMIN_EMAIL = "admin@example.test"


def make_env(tmp_path: Path, **kw) -> Env:
    return build_env(tmp_path, **kw)


def make_admin_user(env: Env, email: str = ADMIN_EMAIL) -> int:
    uid = make_user(env, email, name="Админ")
    conn = env.db()
    try:
        conn.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    return uid


def login_admin(env: Env, email: str = ADMIN_EMAIL) -> int:
    uid = make_admin_user(env, email)
    r = login(env, email, PASSWORD)
    assert r.status_code == 303, r.text
    return uid


def add_vacancy(env: Env, moderation_status: str | None = None, **kw) -> int:
    kw.setdefault("profession", "бариста")
    kw.setdefault("city", "Донецк")
    kw.setdefault("category", "общепит")
    kw.setdefault("summary", "Готовить кофе и общаться с гостями.")
    kw.setdefault("contacts", {"phones": ["+79490000000"]})
    kw.setdefault("source_type", "telegram")
    kw.setdefault("source", "@demo")
    kw.setdefault("source_url", "https://t.me/demo/1")
    kw.setdefault("published_at", "2026-10-29T11:30:00+00:00")
    kw.setdefault("last_seen_at", "2026-10-29T11:30:00+00:00")
    conn = connect(env.db_path)
    try:
        vid = save_vacancy(conn, Vacancy(**kw))
        if moderation_status:
            conn.execute("UPDATE vacancies SET moderation_status=? WHERE id=?", (moderation_status, vid))
            conn.commit()
        return vid
    finally:
        conn.close()


def apost(env: Env, path: str, data: dict | None = None, *, page: str = "/admin", follow: bool = False, **kw):
    """POST админской формы с актуальным токеном CSRF."""
    token = csrf_of(env, page)
    return env.client.post(path, data={"csrf_token": token, **(data or {})}, follow_redirects=follow, **kw)


def rows(env: Env, sql: str, *args):
    return env.query(sql, *args)


def one(env: Env, sql: str, *args):
    r = env.query(sql, *args)
    return r[0] if r else None


def meta(html: str, name: str) -> str:
    m = re.search(rf'<meta name="{name}" content="([^"]*)"', html)
    return m.group(1) if m else ""
