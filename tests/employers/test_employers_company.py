"""Страница компании, значок проверки, метки доверия, честность для собранных вакансий, логотип."""
from __future__ import annotations

import io
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rabota.accounts.mailer import MemoryMailer
from rabota.accounts.ratelimit import RateLimiter
from rabota.employers import pages
from rabota.web.app import create_app
from tests.accounts.harness import SECRET, SITE, START, Clock, Env, login_as
from tests.cabinet.conftest import make_row
from tests.web.postkit import csrf

VERIFIED = "2026-10-01T10:00:00+00:00"


def build(tmp_path) -> Env:
    """Как build_env, но часы страниц тоже стоят на START: метки свежести предсказуемы."""
    clock = Clock()
    mail = MemoryMailer()
    app = create_app(tmp_path / "auth.db", site_url=SITE, now=clock, auth_options={
        "mailer": mail, "secret": SECRET, "clock": clock, "cabinet_stubs": True,
        "limiter": RateLimiter(None, clock=lambda: (clock.current - START).total_seconds())})
    return Env(app, TestClient(app, base_url="http://testserver"), mail, clock, tmp_path / "auth.db", app.state.accounts)


@pytest.fixture
def env(tmp_path):
    e = build(tmp_path)
    with e.client:
        yield e


def seed_company(env, *, verified=False, **cols) -> tuple[int, int]:
    """Работодатель с компанией. Возвращает (id пользователя, id компании)."""
    from tests.accounts.harness import make_user

    uid = make_user(env, "boss@example.test", role="employer", name="Олег")
    conn = env.db()
    fields = {"owner_user_id": uid, "name": "Кофейня Зерно", "description": "Сеть кофеен в центре Донецка.",
              "site": "https://zerno.example", "city": "Донецк", "created_at": "2026-08-15 09:00:00", **cols}
    if verified:
        fields["verified_at"] = VERIFIED
    cur = conn.execute(f"INSERT INTO companies({','.join(fields)}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
    conn.commit()
    conn.close()
    return uid, int(cur.lastrowid)


def add_vac(env, uid, company_id, **kw) -> int:
    conn = env.db()
    try:
        return make_row(conn, uid, company_id=company_id, **kw)
    finally:
        conn.close()


def ld_of(html: str) -> list[dict]:
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)]


# ------------------------------------------------------------------ страница компании

def test_company_page_has_everything(env):
    uid, cid = seed_company(env, verified=True)
    add_vac(env, uid, cid)
    html = env.client.get(f"/company/{cid}").text
    assert html.count("<h1") == 1 and "Кофейня Зерно" in html
    assert "Сеть кофеен в центре Донецка." in html and "https://zerno.example" in html
    assert "На сайте с августа 2026" in html  # дата регистрации
    assert "Вакансии работодателя" in html and "Бариста" in html
    assert "+7 (949) 000-00-00" in html or "+79490000000" in html  # телефон из открытой вакансии
    assert 'rel="canonical"' in html and 'content="noindex' not in html


def test_company_jsonld_is_organization_with_logo_and_phone(env):
    uid, cid = seed_company(env, verified=True)
    add_vac(env, uid, cid)
    types = [d["@type"] for d in ld_of(env.client.get(f"/company/{cid}").text)]
    assert "Organization" in types and "BreadcrumbList" in types
    org = next(d for d in ld_of(env.client.get(f"/company/{cid}").text) if d["@type"] == "Organization")
    assert org["name"] == "Кофейня Зерно" and org["url"] == f"{SITE}/company/{cid}" and org["telephone"].startswith("+7")


def test_company_without_open_vacancies_is_noindex_with_empty_state(env):
    _, cid = seed_company(env)
    html = env.client.get(f"/company/{cid}").text
    assert "noindex" in html and "Открытых вакансий нет" in html


def test_company_without_description_shows_hint(env):
    uid, cid = seed_company(env, description="")
    add_vac(env, uid, cid)
    assert "пока не добавил описание" in env.client.get(f"/company/{cid}").text


def test_unknown_company_page_is_a_stub_not_an_error(env):
    r = env.client.get("/company/777")
    assert r.status_code == 200 and "noindex" in r.text and "ещё оформляется" in r.text


def test_blocked_company_is_hidden(env):
    _, cid = seed_company(env, blocked_at=VERIFIED)
    assert env.client.get(f"/company/{cid}").status_code == 404


# ------------------------------------------------------------------ проверенный работодатель

def test_verified_badge_only_with_verified_at(env):
    _, cid = seed_company(env, verified=True)
    html = env.client.get(f"/company/{cid}").text
    assert "Проверенный работодатель" in html and "Это не гарантия" in html and "(1 октября)" in html
    assert "Не проверен администрацией" not in html


def test_unverified_company_has_no_badge(env):
    uid, cid = seed_company(env)
    add_vac(env, uid, cid)
    html = env.client.get(f"/company/{cid}").text
    assert "Проверенный работодатель" not in html and "Не проверен администрацией" in html
    assert "Проверенный работодатель" not in env.client.get(f"/vacancy/{add_vac(env, uid, cid, profession='кассир')}").text


def test_legacy_verified_flag_is_carried_over_to_verified_at(env):
    """Старая отметка verified при миграции превращается в дату проверки, значок остаётся."""
    _, cid = seed_company(env, verified=False)
    conn = env.db()
    conn.execute("UPDATE companies SET verified = 1 WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    env.db().close()  # новое подключение повторяет миграцию
    assert "Проверенный работодатель" in env.client.get(f"/company/{cid}").text


def test_verified_badge_on_vacancy_page_and_description(env):
    uid, cid = seed_company(env, verified=True)
    vid = add_vac(env, uid, cid)
    html = env.client.get(f"/vacancy/{vid}").text
    assert "Проверенный работодатель" in html
    assert "Проверенный работодатель." in env.client.get(f"/company/{cid}").text  # и в описании страницы


# ------------------------------------------------------------------ показатели

def test_stats_hidden_without_data(env):
    uid, cid = seed_company(env)
    add_vac(env, uid, cid)
    assert "Как работодатель отвечает" not in env.client.get(f"/company/{cid}").text


def test_stats_shown_with_data(env):
    from tests.accounts.harness import make_user

    uid, cid = seed_company(env)
    vid = add_vac(env, uid, cid)
    seekers = [make_user(env, f"s{i}@example.test") for i in range(3)]
    conn = env.db()
    for s in seekers:
        conn.execute(
            "INSERT INTO applications(vacancy_id, user_id, status, created_at, status_changed_at) VALUES (?,?,?,?,?)",
            (vid, s, "viewed", "2026-10-27 10:00:00", "2026-10-27 12:00:00"))
    conn.commit()
    conn.close()
    html = env.client.get(f"/company/{cid}").text
    assert "Как работодатель отвечает" in html and "Откликов получено" in html and "около 2 часов" in html


# ------------------------------------------------------------------ собранные вакансии и доверие

def test_parsed_vacancy_is_honest(env):
    conn = env.db()
    vid = make_row(conn, None, origin="parsed", source_type="telegram", source="@demo", source_url="https://t.me/demo/1",
                   moderation="approved", employer="МКД-Трейд")
    conn.close()
    html = env.client.get(f"/vacancy/{vid}").text
    assert "Собрано из открытых источников" in html and "https://t.me/demo/1" in html
    assert "Работодатель не подтверждён" in html
    assert "Проверена" not in html  # собранной вакансии метка проверки не положена
    assert "Как отличить мошенников" in html and "Пожаловаться на вакансию" in html


def test_employer_card_is_one_big_link_only_with_company(env):
    uid, cid = seed_company(env)
    vid = add_vac(env, uid, cid)
    html = env.client.get(f"/vacancy/{vid}").text
    assert "employer-card--link" in html and f'class="link-arrow employer-card__link" href="/company/{cid}"' in html
    conn = env.db()
    bare = make_row(conn, None, origin="parsed", source_type="telegram", source="@demo", moderation="approved", employer="МКД-Трейд")
    conn.close()
    assert "employer-card--link" not in env.client.get(f"/vacancy/{bare}").text


def test_parsed_vacancy_card_has_source_badge_in_feed(env):
    conn = env.db()
    make_row(conn, None, origin="parsed", source_type="telegram", source="@demo", source_url="https://t.me/demo/1")
    conn.close()
    assert "Из открытых источников" in env.client.get("/vacancies").text


def test_direct_vacancy_has_checked_and_new_labels(env):
    uid, cid = seed_company(env)
    vid = add_vac(env, uid, cid, seen=START - timedelta(hours=2), created_at=(START - timedelta(hours=2)).isoformat())
    feed = env.client.get("/vacancies").text
    assert "Проверена" in feed and "Новая" in feed
    page = env.client.get(f"/vacancy/{vid}").text
    assert "Проверена" in page and "Собрано из открытых источников" not in page


def test_updated_label_for_raised_vacancy(env):
    uid, cid = seed_company(env)
    add_vac(env, uid, cid, seen=START - timedelta(hours=3), published_at=(START - timedelta(days=5)).isoformat(),
            created_at=(START - timedelta(days=5)).isoformat())
    feed = env.client.get("/vacancies").text
    assert "обновлено 3 часа назад" in feed and "Новая" not in feed


def test_pending_vacancy_is_not_labeled_checked(env):
    uid, cid = seed_company(env)
    vid = add_vac(env, uid, cid, moderation="pending", status="hidden")
    assert env.client.get(f"/vacancy/{vid}").status_code == 404
    assert "Проверена" not in env.client.get("/vacancies").text


def test_complaint_button_and_scam_block_are_prominent(env):
    uid, cid = seed_company(env)
    vid = add_vac(env, uid, cid)
    html = env.client.get(f"/vacancy/{vid}").text
    assert "complain__btn" in html and 'id="complain"' in html and "Как отличить мошенников" in html
    assert 'action="/vacancy/' in html and "/complaint" in html


# ------------------------------------------------------------------ автокарточка без владельца

@pytest.mark.parametrize("row,expected", [
    ({"auto_created": 1}, True),
    ({"origin": "auto", "owner_user_id": None}, True),
    ({"origin": "auto", "claimed_user_id": 5}, False),
    ({"origin": "parsed"}, True),
    ({"owner_user_id": 3, "name": "x"}, False),
    ({}, False),
    (None, False),
])
def test_is_auto_rules(row, expected):
    assert pages.is_auto(row) is expected


def test_auto_company_shows_notice_and_claim_button(env):
    uid, cid = seed_company(env)
    conn = env.db()
    conn.execute("ALTER TABLE companies ADD COLUMN auto_created INTEGER")
    conn.execute("UPDATE companies SET auto_created = 1 WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    add_vac(env, uid, cid)
    html = env.client.get(f"/company/{cid}").text
    assert "Карточка создана автоматически, работодатель не подтверждён" in html
    assert f'href="/company/{cid}/claim"' in html and "Это моя компания" in html
    assert "Проверенный работодатель" not in html


def test_regular_company_has_no_claim_button(env):
    uid, cid = seed_company(env)
    add_vac(env, uid, cid)
    assert "Это моя компания" not in env.client.get(f"/company/{cid}").text


def test_claim_page_is_closed_from_search(env):
    _, cid = seed_company(env)
    r = env.client.get(f"/company/{cid}/claim", follow_redirects=False)
    assert r.status_code in (200, 303) and "noindex" in r.headers.get("x-robots-tag", "")


# ------------------------------------------------------------------ логотип

def png(size=(300, 200), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def upload(env, data: bytes, name="logo.png", ctype="image/png", token=None, path="/cabinet/company/logo"):
    token = token if token is not None else csrf(env.client, "/cabinet/company")
    return env.client.post(path, data={"csrf_token": token}, files={"logo": (name, data, ctype)}, follow_redirects=False)


@pytest.fixture
def boss(env):
    uid = login_as(env, "boss@example.test", role="employer", name="Олег")
    conn = env.db()
    cur = conn.execute("INSERT INTO companies(owner_user_id, name, city) VALUES (?, 'Кофейня Зерно', 'Донецк')", (uid,))
    conn.commit()
    conn.close()
    return env, uid, int(cur.lastrowid)


def test_logo_upload_serve_and_show(boss):
    env, _, cid = boss
    r = upload(env, png())
    assert r.status_code == 303 and "logo=ok" in r.headers["location"]
    served = env.client.get(f"/company/{cid}/logo")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    img = Image.open(io.BytesIO(served.content))
    assert img.format == "PNG" and max(img.size) <= 256
    assert f"/company/{cid}/logo?v=" in env.client.get(f"/company/{cid}").text
    assert f"/company/{cid}/logo?v=" in env.client.get("/cabinet/company").text


@pytest.mark.parametrize("data,ctype,code", [
    (b"", "image/png", "no_file"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml", "broken"),
    (b"MZ" + b"\x00" * 300, "image/png", "broken"),
    (b"GIF89a" + b"\x00" * 60, "image/gif", "broken"),
])
def test_bad_logo_is_refused_with_reason(boss, data, ctype, code):
    env, _, cid = boss
    r = upload(env, data, ctype=ctype)
    assert r.status_code == 303 and f"logo_err={code}" in r.headers["location"]
    assert env.client.get(f"/company/{cid}/logo").status_code == 404
    page = env.client.get(r.headers["location"]).text
    assert 'role="alert"' in page or "notice--warn" in page


def test_oversized_logo_is_refused(boss):
    env, _, _ = boss
    r = upload(env, b"\x89PNG" + b"0" * (2 * 1024 * 1024 + 100_000))
    assert r.status_code == 413


def test_polyglot_is_reencoded_without_payload(boss):
    env, _, cid = boss
    payload = b"<?php system($_GET['c']); ?><script>alert(1)</script>"
    upload(env, png() + payload)
    body = env.client.get(f"/company/{cid}/logo").content
    assert payload not in body and body.startswith(b"\x89PNG")


def test_logo_upload_needs_csrf_and_login(boss):
    env, _, cid = boss
    assert upload(env, png(), token="bad").status_code == 403
    assert env.client.get(f"/company/{cid}/logo").status_code == 404
    env.client.cookies.clear()
    r = upload(env, png(), token="x")
    assert r.status_code in (303, 401, 403)
    assert env.client.get(f"/company/{cid}/logo").status_code == 404


def test_seeker_cannot_upload_logo(env):
    login_as(env, "anna@example.test", role="job_seeker", name="Анна")
    r = upload(env, png(), token=csrf(env.client, "/account"))
    assert r.status_code in (303, 403)
    assert not (env.db_path.parent / "logos").exists() or not list((env.db_path.parent / "logos").iterdir())


def test_logo_delete(boss):
    env, _, cid = boss
    upload(env, png())
    r = env.client.post("/cabinet/company/logo/delete", data={"csrf_token": csrf(env.client, "/cabinet/company")},
                        follow_redirects=False)
    assert r.status_code == 303 and "logo=removed" in r.headers["location"]
    assert env.client.get(f"/company/{cid}/logo").status_code == 404
    assert env.client.post("/cabinet/company/logo/delete", data={}, follow_redirects=False).status_code == 403


def test_blocked_company_logo_is_not_served(boss):
    env, _, cid = boss
    upload(env, png())
    conn = env.db()
    conn.execute("UPDATE companies SET blocked_at = ? WHERE id = ?", (VERIFIED, cid))
    conn.commit()
    conn.close()
    assert env.client.get(f"/company/{cid}/logo").status_code == 404


def test_logo_replacement_changes_version(boss):
    env, _, cid = boss
    upload(env, png())
    first = re.search(r"logo\?v=(\w+)", env.client.get(f"/company/{cid}").text)
    upload(env, png(color=(10, 200, 10)))
    second = re.search(r"logo\?v=(\w+)", env.client.get(f"/company/{cid}").text)
    assert first and second and first.group(1) != second.group(1)
