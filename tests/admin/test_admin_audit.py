from rabota.admin import audit
from tests.admin.helpers import ADMIN, NOW


def test_log_and_read(conn):
    audit.log(conn, ADMIN, "vacancy.approve", "vacancy", 5, {"было": "pending"}, ip="10.0.0.1", now=NOW)
    rows = audit.recent(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["actor_email"] == "admin@example.test" and r["action"] == "vacancy.approve"
    assert r["target_type"] == "vacancy" and r["target_id"] == "5"
    assert "pending" in r["details"] and r["ip"] == "10.0.0.1" and r["at"].startswith("2026-10-29T12:00")


def test_system_actor(conn):
    audit.log(conn, None, "vacancy.autohide", "vacancy", 9, "3 жалобы", now=NOW)
    r = audit.recent(conn)[0]
    assert r["actor_user_id"] is None and r["actor_email"] == "система"


def test_newest_first_and_filter(conn):
    audit.log(conn, ADMIN, "a.one", "x", 1, now=NOW)
    audit.log(conn, ADMIN, "b.two", "x", 2, now=NOW)
    assert [r["action"] for r in audit.recent(conn)] == ["b.two", "a.one"]
    assert [r["action"] for r in audit.recent(conn, action_prefix="a.")] == ["a.one"]
    assert audit.count(conn) == 2


def test_details_do_not_store_passwords():
    from rabota.admin.audit import scrub

    assert "secret" not in str(scrub({"password": "secret", "csrf_token": "abc", "note": "ok"}))
    assert scrub({"note": "ok"}) == {"note": "ok"}
