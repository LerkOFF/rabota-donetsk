"""Жалобы на вакансии: приём, автоскрытие, действия."""
import pytest

from rabota.admin import audit, complaints, moderation, stoplist
from rabota.web import queries
from tests.admin.helpers import ADMIN, NOW, row


def visible(conn, vid):
    return bool(conn.execute(f"SELECT 1 FROM vacancies WHERE id=? AND {queries.VISIBLE_SQL}", (vid,)).fetchone())


def complain(conn, vid, key, code="fraud", email=None, text="Мошенничество или просьба заплатить за работу"):
    return complaints.add(conn, vid, text, code=code, reporter_email=email, reporter_key=key, now=NOW)


def test_public_reasons_cover_required_codes():
    codes = {c for c, _ in complaints.PUBLIC_REASONS}
    assert {"fraud", "wrong_data", "closed", "duplicate", "foreign_data"} <= codes
    assert all(t for _, t in complaints.PUBLIC_REASONS)
    assert complaints.code_for_text("Мошенничество или просьба заплатить за работу: детали") == "fraud"
    assert complaints.code_for_text("что-то непонятное") == "other"


def test_add_stores_fields(conn, add):
    vid = add()
    cid = complain(conn, vid, "k1", email="a@example.test")
    r = conn.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    assert r["reporter_email"] == "a@example.test" and r["reporter_key"] == "k1" and r["reason_code"] == "fraud"
    assert r["status"] == "new"


def test_reporter_key_is_stable_and_not_the_ip():
    k = complaints.reporter_key(b"secret", "10.0.0.5")
    assert k == complaints.reporter_key(b"secret", "10.0.0.5")
    assert k != complaints.reporter_key(b"secret", "10.0.0.6")
    assert "10.0.0.5" not in k and len(k) == 24


def test_autohide_needs_distinct_people(conn, add):
    vid = add()
    for _ in range(5):
        complain(conn, vid, "same")
    assert not complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)
    assert visible(conn, vid)


def test_autohide_at_threshold(conn, add):
    vid = add()
    complain(conn, vid, "a")
    complain(conn, vid, "b")
    assert not complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)
    complain(conn, vid, "c")
    assert complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)
    r = row(conn, vid)
    assert r["status"] == "hidden" and "автоматически" in r["moderation_note"].lower()
    assert not visible(conn, vid)
    log = audit.recent(conn)[0]
    assert log["action"] == "vacancy.autohide" and log["actor_user_id"] is None


def test_autohide_threshold_from_config(conn, add, monkeypatch):
    vid = add()
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "2")
    complain(conn, vid, "a")
    complain(conn, vid, "b")
    assert complaints.autohide_if_needed(conn, vid, now=NOW)


def test_autohide_zero_disables(conn, add, monkeypatch):
    vid = add()
    monkeypatch.setenv("COMPLAINT_AUTOHIDE_THRESHOLD", "0")
    for k in "abcdef":
        complain(conn, vid, k)
    assert not complaints.autohide_if_needed(conn, vid, now=NOW)


def test_autohide_ignores_closed_complaints(conn, add):
    vid = add()
    old = [complain(conn, vid, k) for k in "ab"]
    for cid in old:
        complaints.resolve(conn, cid, "dismiss", ADMIN, now=NOW)
    complain(conn, vid, "c")
    assert not complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)


def test_autohide_does_not_touch_hidden_or_pending(conn, add):
    vid = add(status="hidden")
    for k in "abc":
        complain(conn, vid, k)
    assert not complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)


def test_queue_open_first_with_counts(conn, add):
    v1, v2 = add(), add(profession="повар")
    complain(conn, v1, "a")
    complain(conn, v1, "b")
    c3 = complain(conn, v2, "c", code="closed")
    complaints.resolve(conn, c3, "dismiss", ADMIN, now=NOW)
    page = complaints.queue(conn, "open")
    assert page.total == 2 and {i["vacancy_id"] for i in page.items} == {v1}
    assert page.items[0]["same_vacancy_open"] == 2
    assert complaints.queue(conn, "closed").total == 1
    assert complaints.counts(conn)["open"] == 2


def test_resolve_hide_closes_all_open_for_vacancy(conn, add):
    vid = add()
    c1, c2 = complain(conn, vid, "a"), complain(conn, vid, "b")
    complaints.resolve(conn, c1, "hide", ADMIN, now=NOW, note="подтверждено")
    assert row(conn, vid)["status"] == "hidden" and not visible(conn, vid)
    for cid in (c1, c2):
        r = conn.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
        assert r["status"] == "resolved" and r["resolution"] == "hidden" and r["resolved_at"]
    assert audit.recent(conn)[0]["action"] == "complaint.hide"


def test_resolve_dismiss_only_this_one(conn, add):
    vid = add()
    c1, c2 = complain(conn, vid, "a"), complain(conn, vid, "b")
    complaints.resolve(conn, c1, "dismiss", ADMIN, now=NOW, note="не подтвердилось")
    assert conn.execute("SELECT status FROM complaints WHERE id=?", (c1,)).fetchone()[0] == "dismissed"
    assert conn.execute("SELECT status FROM complaints WHERE id=?", (c2,)).fetchone()[0] == "new"
    assert visible(conn, vid)


def test_dismiss_can_restore_autohidden(conn, add):
    vid = add()
    cs = [complain(conn, vid, k) for k in "abc"]
    complaints.autohide_if_needed(conn, vid, threshold=3, now=NOW)
    assert not visible(conn, vid)
    complaints.resolve(conn, cs[0], "dismiss", ADMIN, now=NOW, restore=True)
    assert visible(conn, vid)


def test_resolve_stoplist_adds_entries_hides_and_closes(conn, add):
    vid = add(contacts={"phones": ["+79490000000"], "telegram": "@scammer"}, employer="Тёмные дела")
    cid = complain(conn, vid, "a")
    other = add(profession="курьер", contacts={"phones": ["+79490000000"]})
    complaints.resolve(conn, cid, "stoplist", ADMIN, now=NOW,
                       stop=[("phone", "+79490000000"), ("nick", "@scammer"), ("keyword", "предоплата")])
    kinds = {e["kind"] for e in stoplist.listing(conn)}
    assert kinds == {"phone", "nick", "keyword"}
    assert not visible(conn, vid) and not visible(conn, other)  # скрыты и остальные с тем же телефоном
    r = conn.execute("SELECT status, resolution FROM complaints WHERE id=?", (cid,)).fetchone()
    assert (r["status"], r["resolution"]) == ("resolved", "stoplisted")


def test_resolve_stoplist_rejects_bad_entry_without_partial_changes(conn, add):
    vid = add()
    cid = complain(conn, vid, "a")
    with pytest.raises(ValueError):
        complaints.resolve(conn, cid, "stoplist", ADMIN, now=NOW, stop=[("phone", "+79490000000"), ("phone", "12")])
    assert stoplist.listing(conn) == [] and visible(conn, vid)


def test_resolve_unknown_action(conn, add):
    cid = complain(conn, add(), "a")
    with pytest.raises(ValueError):
        complaints.resolve(conn, cid, "explode", ADMIN, now=NOW)


def test_candidates_for_stoplist(conn, add):
    vid = add(contacts={"phones": ["+79490000000", "+79491112233"], "telegram": "@x_hr"}, employer="ООО Х")
    c = complaints.stop_candidates(conn, vid)
    assert [p["value"] for p in c if p["kind"] == "phone"] == ["+79490000000", "+79491112233"]
    assert {p["kind"] for p in c} == {"phone", "nick", "employer"}


def test_reply_builds_mail_and_marks(conn, add):
    cid = complain(conn, add(), "a", email="user@example.test")
    mail = complaints.build_reply(conn, cid, "Спасибо, вакансию скрыли.", site_url="https://example.test")
    assert mail.to == "user@example.test" and "скрыли" in mail.body and "Работа Донецк" in mail.subject
    complaints.mark_replied(conn, cid, ADMIN, NOW)
    assert conn.execute("SELECT reply_sent_at FROM complaints WHERE id=?", (cid,)).fetchone()[0]
    assert audit.recent(conn)[0]["action"] == "complaint.reply"


def test_reply_requires_address_and_text(conn, add):
    cid = complain(conn, add(), "a")
    with pytest.raises(ValueError):
        complaints.build_reply(conn, cid, "текст", site_url="https://example.test")
    cid2 = complain(conn, add(), "b", email="u@example.test")
    with pytest.raises(ValueError):
        complaints.build_reply(conn, cid2, "   ", site_url="https://example.test")


def test_moderation_hide_via_complaints_keeps_pending_flow(conn, add):
    add(status="hidden", moderation_status="pending")
    assert moderation.queue(conn, "pending").total == 1
