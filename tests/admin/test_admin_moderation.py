"""Модерация вакансий работодателей: очередь, одобрить, отклонить, скрыть, вернуть, правка."""
import pytest

from rabota.admin import audit, moderation
from rabota.web import queries
from tests.admin.helpers import ADMIN, NOW, row


def visible_ids(conn):
    return [r[0] for r in conn.execute(f"SELECT id FROM vacancies WHERE {queries.VISIBLE_SQL}")]


@pytest.fixture
def pending(add, conn):
    vid = add(status="hidden", moderation_status="pending", source_type="site", source="razmestit",
              contacts={"phones": ["+79490000000"], "email": "boss@example.test"})
    return vid


def test_queue_lists_only_pending_by_default(conn, add, pending):
    add()  # обычная опубликованная
    rejected = add(status="hidden", moderation_status="rejected")
    page = moderation.queue(conn, "pending")
    assert [v["id"] for v in page.items] == [pending]
    assert [v["id"] for v in moderation.queue(conn, "rejected").items] == [rejected]
    assert page.total == 1


def test_queue_views_and_search(conn, add, pending):
    hid = add(status="hidden", profession="грузчик", employer="Ромашка")
    assert [v["id"] for v in moderation.queue(conn, "hidden").items] == [hid]
    assert [v["id"] for v in moderation.queue(conn, "all", q="ромашк").items] == [hid]
    assert [v["id"] for v in moderation.queue(conn, "all", q=str(pending)).items] == [pending]
    assert moderation.queue(conn, "all", q="нет такого").total == 0


def test_queue_counts(conn, add, pending):
    add(status="hidden", moderation_status="pending")
    c = moderation.counts(conn)
    assert c["pending"] == 2


def test_pending_not_visible_until_approved(conn, pending):
    assert pending not in visible_ids(conn)
    assert moderation.approve(conn, pending, ADMIN, NOW)
    r = row(conn, pending)
    assert (r["status"], r["moderation_status"]) == ("active", "approved")
    assert r["last_seen_at"].startswith("2026-10-29T12:00")  # срок жизни пошёл с одобрения
    assert pending in visible_ids(conn)


def test_approve_writes_audit(conn, pending):
    moderation.approve(conn, pending, ADMIN, NOW)
    r = audit.recent(conn)[0]
    assert r["action"] == "vacancy.approve" and r["target_id"] == str(pending)


def test_approve_unknown_returns_false(conn):
    assert not moderation.approve(conn, 999, ADMIN, NOW)


def test_reject_needs_reason_and_hides(conn, pending):
    with pytest.raises(ValueError):
        moderation.reject(conn, pending, ADMIN, "  ", NOW)
    moderation.reject(conn, pending, ADMIN, moderation.REJECT_TEMPLATES["paid"], NOW)
    r = row(conn, pending)
    assert r["moderation_status"] == "rejected" and r["status"] == "hidden"
    assert "заплатить" in r["moderation_note"]
    assert pending not in visible_ids(conn)


def test_reject_template_lookup():
    assert moderation.reason_text("paid", "") == moderation.REJECT_TEMPLATES["paid"]
    assert moderation.reason_text("other", "свой текст") == "свой текст"
    assert moderation.reason_text("paid", "свой") == "свой"  # ручной текст главнее шаблона
    assert moderation.reason_text("", "") == ""


def test_hide_and_restore_published(conn, add):
    vid = add()
    assert vid in visible_ids(conn)
    moderation.hide(conn, vid, ADMIN, "проверка", NOW)
    assert vid not in visible_ids(conn) and row(conn, vid)["status"] == "hidden"
    moderation.restore(conn, vid, ADMIN, NOW)
    assert vid in visible_ids(conn) and row(conn, vid)["moderation_note"] is None


def test_restore_does_not_publish_unmoderated(conn, pending):
    assert not moderation.restore(conn, pending, ADMIN, NOW)
    assert row(conn, pending)["status"] == "hidden"


def test_hide_pending_keeps_it_out_of_visible(conn, pending):
    moderation.hide(conn, pending, ADMIN, "x", NOW)
    assert pending not in visible_ids(conn)


def test_update_fields_changes_and_audits(conn, pending):
    changed = moderation.update_fields(conn, pending, ADMIN, {
        "profession": "старший бариста", "pay_min": 5000, "summary": "Новый текст описания вакансии."}, NOW)
    assert set(changed) == {"profession", "pay_min", "summary"}
    r = row(conn, pending)
    assert r["profession"] == "старший бариста" and r["pay_min"] == 5000
    log = audit.recent(conn)[0]
    assert log["action"] == "vacancy.edit" and "profession" in log["details"]


def test_update_fields_ignores_unknown_and_unchanged(conn, pending):
    assert moderation.update_fields(conn, pending, ADMIN, {"status": "active", "id": 5, "profession": "бариста"}, NOW) == []
    assert row(conn, pending)["status"] == "hidden"


def test_update_contacts_json(conn, pending):
    moderation.update_fields(conn, pending, ADMIN, {"contacts": {"phones": ["+79491112233"]}}, NOW)
    assert "+79491112233" in row(conn, pending)["contacts"]


def test_notify_address_prefers_owner_account(conn, add, pending):
    conn.execute("INSERT INTO users(id, email, password_hash, role) VALUES (10, 'owner@example.test', 'x', 'employer')")
    conn.execute("UPDATE vacancies SET owner_user_id=10 WHERE id=?", (pending,))
    assert moderation.notify_address(conn, pending) == "owner@example.test"


def test_notify_address_falls_back_to_contact_email(conn, pending):
    assert moderation.notify_address(conn, pending) == "boss@example.test"


def test_stoplisted_flag(conn, pending):
    from rabota.admin import stoplist

    assert moderation.stoplist_match(conn, pending) is None
    stoplist.add(conn, "phone", "+79490000000", actor=ADMIN)
    m = moderation.stoplist_match(conn, pending)
    assert m and m.kind == "phone"
