"""Жалобы на сообщения, пользователей и резюме (таблица reports): очередь и действия."""
import pytest

from rabota.admin import audit, complaints
from tests.admin.helpers import ADMIN, NOW


@pytest.fixture
def world(conn):
    conn.execute("INSERT INTO users(id,email,password_hash,role) VALUES (1,'a@x.ru','h','admin'),(2,'e@x.ru','h','employer'),(3,'s@x.ru','h','job_seeker')")
    conn.execute("INSERT INTO conversations(id,employer_user_id,seeker_user_id,started_by) VALUES (1,2,3,2)")
    conn.execute("INSERT INTO messages(id,conversation_id,sender_user_id,body) VALUES (1,1,2,'Переведите 500 рублей за оформление')")
    conn.execute("INSERT INTO reports(id,reporter_user_id,target_type,target_id,reason) VALUES (1,3,'message',1,'Просит деньги')")
    conn.execute("INSERT INTO reports(id,reporter_user_id,target_type,target_id,reason) VALUES (2,3,'user',2,'Спам')")
    conn.commit()
    return conn


def test_queue_shows_target_details(world):
    page = complaints.reports_queue(world, "open")
    assert page.total == 2
    msg = next(r for r in page.items if r["target_type"] == "message")
    assert "500 рублей" in msg["target_text"] and msg["target_user_id"] == 2 and msg["reporter_email"] == "s@x.ru"
    usr = next(r for r in page.items if r["target_type"] == "user")
    assert usr["target_user_id"] == 2 and "e@x.ru" in usr["target_text"]


def test_dismiss_and_resolve(world):
    complaints.report_resolve(world, 1, "dismiss", ADMIN, NOW, note="шутка")
    r = world.execute("SELECT status, resolution, admin_note FROM reports WHERE id=1").fetchone()
    assert (r["status"], r["resolution"], r["admin_note"]) == ("dismissed", "dismissed", "шутка")
    assert complaints.reports_queue(world, "open").total == 1
    assert audit.recent(world)[0]["action"] == "report.dismiss"


def test_block_user_closes_sessions(world):
    world.execute("INSERT INTO sessions(user_id, token_hash, expires_at) VALUES (2,'h','2099-01-01T00:00:00')")
    world.commit()
    complaints.report_resolve(world, 1, "block", ADMIN, NOW)
    assert world.execute("SELECT status FROM users WHERE id=2").fetchone()[0] == "blocked"
    assert world.execute("SELECT COUNT(*) FROM sessions WHERE user_id=2").fetchone()[0] == 0
    assert world.execute("SELECT status FROM reports WHERE id=1").fetchone()[0] == "resolved"


def test_cannot_block_admin(world):
    world.execute("INSERT INTO reports(id,reporter_user_id,target_type,target_id,reason) VALUES (3,3,'user',1,'x')")
    world.commit()
    with pytest.raises(ValueError):
        complaints.report_resolve(world, 3, "block", ADMIN, NOW)


def test_unknown_action(world):
    with pytest.raises(ValueError):
        complaints.report_resolve(world, 1, "nuke", ADMIN, NOW)
