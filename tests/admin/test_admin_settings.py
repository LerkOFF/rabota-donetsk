"""Настройки админки: схема, проверка значений, запись с журналом, чтение в коде сайта, права и CSRF."""
import re
from datetime import timedelta

import pytest

from rabota import config, lifecycle
from rabota.admin import audit, complaints, settings
from rabota.db import connect
from tests.accounts.harness import PASSWORD, csrf_of, login, make_user
from tests.admin.helpers import ADMIN, NOW
from tests.web.admin_harness import login_admin, make_env

# ------------------------------------------------------------------ схема

def test_schema_is_well_formed():
    keys = [s.key for s in settings.SCHEMA]
    assert len(keys) == len(set(keys))
    for s in settings.SCHEMA:
        assert s.group in settings.GROUPS, s.key
        assert s.label and s.kind in (settings.KIND_INT, settings.KIND_BOOL, settings.KIND_TEXT)
        if s.kind == settings.KIND_INT:
            assert s.minimum <= s.default_value() <= s.maximum, f"{s.key}: умолчание вне границ"


def test_migration_is_repeatable(tmp_path):
    conn = connect(tmp_path / "s.db")
    settings.migrate(conn)
    settings.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "settings_log"} <= names


def test_defaults_come_from_env(conn, monkeypatch):
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "5")
    assert settings.effective(conn, "complaint_autohide_threshold") == 5
    monkeypatch.setenv("REGISTRATION_OPEN", "0")
    assert settings.effective(conn, "registration_open") is False


# ------------------------------------------------------------------ проверка

@pytest.mark.parametrize("key,raw", [
    ("vacancy_ttl_days", "2"), ("vacancy_ttl_days", "91"), ("vacancy_ttl_days", "abc"), ("vacancy_ttl_days", ""),
    ("vacancy_ttl_days", "1.5"), ("complaint_autohide_threshold", "-1"), ("complaint_autohide_threshold", "51"),
    ("registration_open", "maybe"), ("registration_closed_note", "я" * 301),
])
def test_validation_rejects(key, raw):
    values, errors = settings.validate({key: raw})
    assert key in errors and key not in values


def test_validation_accepts_and_ignores_unknown():
    values, errors = settings.validate({"vacancy_ttl_days": " 30 ", "registration_open": "0", "unknown": "1",
                                        "registration_closed_note": "Скоро\nоткроем"})
    assert errors == {}
    assert values == {"vacancy_ttl_days": 30, "registration_open": False, "registration_closed_note": "Скоро откроем"}
    assert "unknown" not in values


# ------------------------------------------------------------------ запись и журнал

def test_save_stores_only_changes_and_logs(conn):
    changes, errors = settings.save(conn, ADMIN, {"vacancy_ttl_days": "30", "complaint_autohide_threshold": "3"}, "1.2.3.4", NOW)
    assert errors == {}
    assert [c["key"] for c in changes] == ["vacancy_ttl_days"]
    assert changes[0]["new"] == "30 дней" and changes[0]["old"] == "21 дней"
    assert settings.effective(conn, "vacancy_ttl_days") == 30
    log = settings.change_log(conn)
    assert len(log) == 1 and log[0]["actor_email"] == ADMIN["email"] and log[0]["ip"] == "1.2.3.4" and log[0]["label"] == "Срок жизни вакансии"
    entry = audit.recent(conn, 1)[0]
    assert entry["action"] == "settings.save" and "vacancy_ttl_days" in entry["details"]


def test_value_equal_to_default_resets_override(conn):
    settings.save(conn, ADMIN, {"vacancy_ttl_days": "30"}, now=NOW)
    assert settings.snapshot(conn)["vacancy_ttl_days"]["changed"] is True
    changes, _ = settings.save(conn, ADMIN, {"vacancy_ttl_days": str(config.VACANCY_TTL_DAYS)}, now=NOW)
    assert len(changes) == 1
    assert settings.snapshot(conn)["vacancy_ttl_days"]["changed"] is False
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert len(settings.change_log(conn)) == 2


def test_nothing_written_on_any_error(conn):
    changes, errors = settings.save(conn, ADMIN, {"vacancy_ttl_days": "30", "complaint_autohide_threshold": "999"}, now=NOW)
    assert changes == [] and "complaint_autohide_threshold" in errors
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert settings.change_log(conn) == []


def test_unchanged_save_writes_no_log(conn):
    changes, errors = settings.save(conn, ADMIN, {"vacancy_ttl_days": str(config.VACANCY_TTL_DAYS)}, now=NOW)
    assert changes == [] and errors == {}
    assert settings.change_log(conn) == []


def test_broken_stored_value_falls_back_to_default(conn):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('vacancy_ttl_days', 'мусор', '2026-01-01')")
    conn.commit()
    assert settings.effective(conn, "vacancy_ttl_days") == config.VACANCY_TTL_DAYS
    conn.execute("UPDATE settings SET value='9999' WHERE key='vacancy_ttl_days'")
    conn.commit()
    assert settings.effective(conn, "vacancy_ttl_days") == config.VACANCY_TTL_DAYS


def test_missing_table_does_not_break_reading():
    import sqlite3

    bare = sqlite3.connect(":memory:")
    assert settings.effective(bare, "vacancy_ttl_days") == config.VACANCY_TTL_DAYS
    assert settings.stored(bare) == {}
    assert settings.change_log(bare) == []
    assert settings.effective(None, "registration_open") is True


# ------------------------------------------------------------------ чтение в коде сайта

def test_lifecycle_expire_uses_table_value(conn, add):
    vid = add(last_seen_at=(NOW - timedelta(days=10)).isoformat(), published_at=(NOW - timedelta(days=10)).isoformat())
    assert lifecycle.expire(conn, now=NOW) == 0
    settings.save(conn, ADMIN, {"vacancy_ttl_days": "7"}, now=NOW)
    assert lifecycle.expire(conn, now=NOW) == 1
    assert conn.execute("SELECT status FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == "expired"


def test_lifecycle_purge_uses_keep_days(conn, add):
    old = (NOW - timedelta(days=40)).isoformat()
    vid = add(last_seen_at=old, published_at=old)
    lifecycle.expire(conn, ttl_days=21, now=NOW)
    assert lifecycle.purge(conn, now=NOW) == 0  # 21 + 30 = 51 дней, прошло 40: ещё просто скрыта
    assert conn.execute("SELECT status FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == "expired"
    settings.save(conn, ADMIN, {"expired_keep_days": "10"}, now=NOW)
    assert lifecycle.purge(conn, now=NOW) == 0  # ушла в архив, а не в корзину
    assert conn.execute("SELECT status FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == "archived"
    settings.save(conn, ADMIN, {"archive_keep_days": "30"}, now=NOW)
    assert lifecycle.purge(conn, now=NOW) == 0
    assert lifecycle.purge(conn, now=NOW + timedelta(days=31)) == 1
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == 0


def test_autohide_threshold_prefers_table(conn, monkeypatch):
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "3")
    assert complaints.autohide_threshold(conn) == 3
    assert complaints.autohide_threshold() == 3
    settings.save(conn, ADMIN, {"complaint_autohide_threshold": "1"}, now=NOW)
    assert complaints.autohide_threshold(conn) == 1


def test_ttl_note_when_above_env(conn):
    settings.save(conn, ADMIN, {"vacancy_ttl_days": "60"}, now=NOW)
    page = settings.page(conn)
    assert page["notes"] and "VACANCY_TTL_DAYS" in page["notes"][0]


# ------------------------------------------------------------------ веб: права, CSRF, форма

@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def fields(**over):
    data = {"s_vacancy_ttl_days": "21", "s_expired_keep_days": "30", "s_complaint_autohide_threshold": "3",
            "s_registration_open": "1", "s_registration_closed_note": ""}
    data.update(over)
    return data


def post_settings(env, data):
    return env.client.post("/admin/settings", data={"csrf_token": csrf_of(env, "/admin/settings"), **data}, follow_redirects=False)


def test_guest_and_non_admin_cannot_open(env):
    assert env.client.get("/admin/settings", follow_redirects=False).status_code == 303
    make_user(env, "seeker@example.test")
    assert login(env, "seeker@example.test", PASSWORD).status_code == 303
    assert env.client.get("/admin/settings").status_code == 404
    r = env.client.post("/admin/settings", data={"s_vacancy_ttl_days": "5"}, follow_redirects=False)
    assert r.status_code in (403, 404)


def test_post_without_csrf_is_rejected_and_changes_nothing(env):
    login_admin(env)
    r = env.client.post("/admin/settings", data=fields(s_vacancy_ttl_days="30"), follow_redirects=False)
    assert r.status_code == 403
    r = env.client.post("/admin/settings", data={"csrf_token": "0" * 64, **fields(s_vacancy_ttl_days="30")}, follow_redirects=False)
    assert r.status_code == 403
    assert env.query("SELECT COUNT(*) FROM settings")[0][0] == 0


def test_get_page_renders_form_and_sortable_markup(env):
    login_admin(env)
    html = env.client.get("/admin/settings").text
    assert 'data-sortable="settings-groups"' in html and 'data-key="vacancies"' in html and 'draggable="true"' in html
    assert 'name="s_vacancy_ttl_days"' in html and "Журнал изменений" in html and "Настройки ещё не меняли" in html


def test_save_highlights_changed_and_shows_log(env):
    login_admin(env)
    r = post_settings(env, fields(s_vacancy_ttl_days="30", s_registration_open="0"))
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/settings")
    html = env.client.get("/admin/settings").text
    assert "adm-set--changed" in html
    assert "Срок жизни вакансии" in html and "30 дней" in html
    assert "Регистрация открыта" in html
    rows = env.query("SELECT key, new FROM settings_log ORDER BY id")
    assert [(r[0], r[1]) for r in rows] == [("vacancy_ttl_days", "30 дней"), ("registration_open", "нет")]
    assert env.query("SELECT COUNT(*) FROM audit_log WHERE action='settings.save'")[0][0] == 1


def test_invalid_value_returns_422_with_message_and_keeps_input(env):
    login_admin(env)
    r = post_settings(env, fields(s_vacancy_ttl_days="500"))
    assert r.status_code == 422
    assert "Допустимо от 3 до 90" in r.text and 'value="500"' in r.text
    assert env.query("SELECT COUNT(*) FROM settings")[0][0] == 0


def test_unchanged_post_says_nothing_changed(env):
    login_admin(env)
    r = post_settings(env, fields())
    assert r.status_code == 303 and "ok=nochange" in r.headers["location"]


def test_registration_switch_changes_site_without_restart(env):
    from fastapi.testclient import TestClient

    login_admin(env)
    guest = TestClient(env.app, base_url="http://testserver")
    assert guest.get("/register").status_code == 200
    post_settings(env, fields(s_registration_open="0", s_registration_closed_note="Откроем в декабре."))
    page = guest.get("/register")
    assert "откроется позже" in page.text and "Откроем в декабре." in page.text
    post_settings(env, fields(s_registration_open="1"))
    assert "откроется позже" not in guest.get("/register").text


def test_autohide_setting_applies_to_new_complaint_threshold(env):
    login_admin(env)
    post_settings(env, fields(s_complaint_autohide_threshold="7"))
    assert "7" in env.client.get("/admin/complaints").text


def test_settings_page_has_seo_block(env):
    login_admin(env)
    r = env.client.get("/admin/settings")
    assert 'rel="canonical" href="https://example.test/admin/settings"' in r.text
    assert re.search(r'<meta name="robots" content="noindex, nofollow"', r.text)
