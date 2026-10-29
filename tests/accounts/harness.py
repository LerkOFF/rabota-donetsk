"""Общая обвязка тестов аккаунтов: приложение на временной базе, управляемые часы, перехват писем."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from rabota.accounts import service
from rabota.accounts.deps import Accounts
from rabota.accounts.mailer import Mail, MemoryMailer
from rabota.web.app import create_app

START = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SITE = "https://example.test"
PASSWORD = "tulip-garden-77"
SECRET = "test-secret-" + "x" * 32


class Clock:
    def __init__(self, start: datetime = START):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kw: float) -> None:
        self.current += timedelta(**kw)


@dataclass
class Env:
    app: Any
    client: TestClient
    mailer: MemoryMailer
    clock: Clock
    db_path: Path
    acc: Accounts

    def db(self) -> sqlite3.Connection:
        return service.open_db(self.db_path)

    def user(self, email: str) -> sqlite3.Row | None:
        conn = self.db()
        try:
            return service.get_user_by_email(conn, email)
        finally:
            conn.close()

    def query(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        conn = self.db()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


def build_env(tmp_path: Path, *, https: bool = False, rate_limits: dict | None = None, mailer: MemoryMailer | None = None,
              cabinet_stubs: bool = True) -> Env:
    db_path = tmp_path / "auth.db"
    mail = mailer or MemoryMailer()
    clock = Clock()
    app = create_app(db_path, site_url=SITE, auth_options={
        "mailer": mail, "secret": SECRET, "clock": clock, "rate_limits": rate_limits, "cabinet_stubs": cabinet_stubs})
    acc = app.state.accounts
    client = TestClient(app, base_url="https://testserver" if https else "http://testserver")
    return Env(app, client, mail, clock, db_path, acc)


def env_impl(tmp_path: Path):
    """Тело фикстуры env. В каждом тестовом модуле: env = pytest.fixture(env_impl)."""
    e = build_env(tmp_path)
    with e.client:
        yield e


# ------------------------------------------------------------------ действия через веб

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(env: Env, path: str = "/login") -> str:
    """Токен формы для текущего посетителя (заодно ставит cookie CSRF)."""
    html = env.client.get(path).text
    m = _CSRF_RE.search(html)
    assert m, f"на {path} нет csrf_token"
    return m.group(1)


def post(env: Env, path: str, data: dict[str, str], *, page: str | None = None, follow: bool = False, **kw: Any):
    """POST формы с актуальным CSRF-токеном, токен берётся со страницы page (по умолчанию тот же путь)."""
    token = csrf_of(env, page or path)
    return env.client.post(path, data={"csrf_token": token, **data}, follow_redirects=follow, **kw)


def reg_data(email: str = "ivan@example.test", **over: str) -> dict[str, str]:
    data = {"email": email, "password": PASSWORD, "name": "Иван", "role": "job_seeker", "consent": "1"}
    data.update(over)
    return data


def register(env: Env, email: str = "ivan@example.test", **over: str):
    return post(env, "/register", reg_data(email, **over))


def link_in(mail: Mail | None) -> str:
    """Путь ссылки из письма (без адреса сайта)."""
    assert mail is not None, "письма нет"
    m = re.search(re.escape(SITE) + r"(/\S+)", mail.body)
    assert m, mail.body
    return m.group(1)


def token_in(mail: Mail | None) -> str:
    return link_in(mail).rsplit("/", 1)[1]


def make_user(env: Env, email: str = "ivan@example.test", *, role: str = "job_seeker", verified: bool = True,
              password: str = PASSWORD, name: str = "Иван") -> int:
    """Пользователь напрямую в базе (минуя веб)."""
    conn = env.db()
    try:
        reg = service.register(conn, email=email, password=password, name=name, role=role, consent=True, now=env.clock())
        if verified:
            assert reg.token
            service.verify_email(conn, reg.token, env.clock())
        return reg.user_id
    finally:
        conn.close()


def login(env: Env, email: str = "ivan@example.test", password: str = PASSWORD, **extra: str):
    return post(env, "/login", {"email": email, "password": password, **extra})


def login_as(env: Env, email: str = "ivan@example.test", *, role: str = "job_seeker", name: str = "Иван") -> int:
    uid = make_user(env, email, role=role, name=name)
    r = login(env, email)
    assert r.status_code == 303, r.text
    return uid
