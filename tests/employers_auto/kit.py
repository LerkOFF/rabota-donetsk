"""Общая обвязка тестов автокарточек: вакансии, карточки, заявки, веб-окружение."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from rabota.db import connect, save_vacancy
from rabota.employers_auto import cards, claims
from rabota.models import Vacancy
from tests.accounts.harness import SECRET, Env, build_env, make_user

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SECRET_BYTES = SECRET.encode()
SITE = "https://example.test"


def stamp(days: float = 0.0, hours: float = 1.0, base: datetime = NOW) -> str:
    return (base - timedelta(days=days, hours=hours)).replace(microsecond=0).isoformat()


def vacancy(conn, employer: str | None = "Кофейня Зерно", **kw: Any) -> int:
    """Собранная вакансия (источник не сайт): returns id."""
    kw.setdefault("profession", "бариста")
    kw.setdefault("city", "Донецк")
    kw.setdefault("category", "общепит")
    kw.setdefault("summary", "Готовить кофе и общаться с гостями.")
    kw.setdefault("contacts", {"phones": ["+79490001122"]})
    kw.setdefault("source_type", "telegram")
    kw.setdefault("source", "@demo")
    kw.setdefault("published_at", stamp())
    kw.setdefault("last_seen_at", stamp())
    if "external_id" not in kw:
        kw["external_id"] = f"{kw['source']}-{conn.execute('SELECT COUNT(*) FROM vacancies').fetchone()[0] + 1}"
    kw.setdefault("source_url", f"https://t.me/demo/{kw['external_id']}")
    return save_vacancy(conn, Vacancy(employer=employer, **kw))


def auto_company(conn, name: str = "Кофейня Зерно", count: int = 1, **kw: Any) -> int:
    """Автокарточка с `count` вакансиями. Возвращает id компании."""
    ids = [vacancy(conn, name, **kw) for _ in range(count)]
    cards.attach_pending(conn, now=NOW)
    row = conn.execute("SELECT company_id FROM vacancies WHERE id = ?", (ids[0],)).fetchone()
    assert row and row[0], "карточка не создана"
    return int(row[0])


def company(conn, company_id: int) -> dict[str, Any]:
    c = cards.get(conn, company_id)
    assert c is not None
    return c


def user_row(conn, user_id: int) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def submit_claim(conn, company_id: int, user_id: int, *, method: str = "phone", contact: str = "+79490001122",
                 message: str = "Я директор", now: datetime = NOW) -> claims.Outcome:
    return claims.submit(conn, SECRET_BYTES, company(conn, company_id), user_row(conn, user_id), method=method, contact=contact,
                         message=message, ip="203.0.113.5", site_url=SITE, now=now)


def make_employer(env: Env, email: str = "boss@example.test", name: str = "Олег") -> int:
    return make_user(env, email, role="employer", name=name)


def open_db(env: Env):
    conn = connect(env.db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


def form_token(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "нет csrf_token"
    return m.group(1)


def env_with_company(tmp_path, name: str = "Кофейня Зерно", count: int = 1) -> tuple[Env, int]:
    env = build_env(tmp_path)
    conn = env.db()
    try:
        cid = auto_company(conn, name, count)
    finally:
        conn.close()
    return env, cid



def raw_post(conn, source_type: str, source: str, external_id: str, extra: dict[str, Any], text: str = "Вакансия") -> None:
    from rabota.db import save_raw
    from rabota.models import RawPost

    save_raw(conn, RawPost(source_type, source, external_id, f"https://example.test/{external_id}", text, stamp(), [], extra))
    conn.commit()
