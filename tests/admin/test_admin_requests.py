"""Запросы по персональным данным: журнал, сроки, удаление по контакту."""
import csv
import io
from datetime import UTC, datetime

import pytest

from rabota.admin import audit, stoplist
from rabota.admin import requests as legal_requests
from tests.admin.helpers import ADMIN, NOW, make_vac, row

FRI = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)  # пятница


def new(conn, kind="delete", **kw):
    kw.setdefault("name", "Иван")
    kw.setdefault("contact", "ivan@example.test")
    kw.setdefault("subject", "https://работа-донецк.рф/vacancy/5")
    kw.setdefault("body", "Удалите мой номер из вакансий.")
    return legal_requests.create(conn, kind, now=NOW, **kw)


def test_working_days_skip_weekends():
    assert legal_requests.add_working_days(FRI, 1).date().isoformat() == "2026-11-02"  # понедельник
    assert legal_requests.add_working_days(FRI, 3).date().isoformat() == "2026-11-04"
    assert legal_requests.add_working_days(NOW, 10).date().isoformat() == "2026-11-12"


def test_due_by_type():
    created = NOW
    assert legal_requests.due_at("vacancy_owner", created).date().isoformat() == "2026-11-03"  # 3 раб. дня
    assert legal_requests.due_at("rectify", created).date().isoformat() == "2026-11-09"  # 7 раб. дней
    assert legal_requests.due_at("access", created).date().isoformat() == "2026-11-12"  # 10 раб. дней
    assert legal_requests.due_at("withdraw", created).date().isoformat() == "2026-11-28"  # 30 дней


def test_create_sets_due_and_audit(conn):
    rid = new(conn, "vacancy_owner")
    r = legal_requests.get(conn, rid, now=NOW)
    assert r["status"] == "new" and r["due_at"].startswith("2026-11-03") and r["channel"] == "web"
    assert r["kind_label"] and not r["overdue"]


def test_create_validates(conn):
    with pytest.raises(ValueError):
        legal_requests.create(conn, "nonsense", now=NOW, name="a", contact="b@c.ru", body="x")
    with pytest.raises(ValueError):
        legal_requests.create(conn, "access", now=NOW, name="", contact="", body="")


def test_overdue_flags(conn):
    rid = new(conn, "vacancy_owner")
    later = datetime(2026, 11, 5, tzinfo=UTC)
    assert legal_requests.get(conn, rid, now=later)["overdue"]
    legal_requests.update(conn, rid, ADMIN, later, status="waiting")
    assert not legal_requests.get(conn, rid, now=later)["overdue"]  # ждём уточнение: срок стоит
    legal_requests.update(conn, rid, ADMIN, later, status="done", decision="Вакансия скрыта", decision_code="satisfied")
    r = legal_requests.get(conn, rid, now=later)
    assert not r["overdue"] and r["answered_at"].startswith("2026-11-05")


def test_due_soon(conn):
    rid = new(conn, "vacancy_owner")
    r = legal_requests.get(conn, rid, now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    assert r["due_soon"] and not r["overdue"]


def test_done_requires_decision(conn):
    rid = new(conn)
    with pytest.raises(ValueError):
        legal_requests.update(conn, rid, ADMIN, NOW, status="done", decision="")


def test_queue_filters_and_counts(conn):
    a = new(conn, "vacancy_owner")
    b = new(conn, "access")
    legal_requests.update(conn, b, ADMIN, NOW, status="done", decision="Отправили выписку", decision_code="satisfied")
    assert [r["id"] for r in legal_requests.queue(conn, "open", now=NOW).items] == [a]
    assert [r["id"] for r in legal_requests.queue(conn, "done", now=NOW).items] == [b]
    assert legal_requests.queue(conn, "all", now=NOW).total == 2
    late = datetime(2026, 11, 20, tzinfo=UTC)
    assert legal_requests.counts(conn, late) == {"open": 1, "overdue": 1, "done": 1}
    assert [r["id"] for r in legal_requests.queue(conn, "overdue", now=late).items] == [a]


def test_overdue_sorted_first(conn):
    new(conn, "access")
    b = new(conn, "vacancy_owner")
    late = datetime(2026, 11, 5, tzinfo=UTC)
    assert legal_requests.queue(conn, "open", now=late).items[0]["id"] == b


def test_manual_entry_channel(conn):
    rid = legal_requests.create(conn, "authority", now=NOW, name="Роскомнадзор", contact="письмо №5", body="запрос",
                                channel="email", actor=ADMIN)
    assert legal_requests.get(conn, rid)["channel"] == "email"
    assert audit.recent(conn)[0]["action"] == "request.create"


def test_erase_by_phone_hides_scrubs_and_stoplists(conn, add):
    a = add(contacts={"phones": ["+79490000000", "+79491112233"]}, source="ch1", external_id="1")
    b = add(profession="повар", contacts={"phones": ["8 949 000 00 00"]}, source="ch2", external_id="2", status="expired")
    c = add(profession="сварщик", contacts={"phones": ["+79495550000"]}, source="ch3", external_id="3")
    for src, ext in (("ch1", "1"), ("ch2", "2"), ("ch3", "3")):
        conn.execute("INSERT INTO raw_posts(source_type,source,external_id,url,text) VALUES ('telegram',?,?,'u','Звоните +79490000000')", (src, ext))
    conn.commit()
    rid = new(conn)
    res = legal_requests.erase_contact(conn, "phone", "+7 949 000-00-00", ADMIN, NOW, request_id=rid)
    assert res.vacancies == 2 and res.raw_posts == 2
    for vid in (a, b):
        r = row(conn, vid)
        assert r["status"] == "hidden" and "персональн" in r["moderation_note"].lower()
        assert "9490000000" not in r["contacts"].replace("+7", "").replace(" ", "")
    assert "+79491112233" in row(conn, a)["contacts"]  # другие контакты остаются
    assert row(conn, c)["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == 1
    # сборщики не вернут: запись в стоп-листе, самого номера в ней нет
    e = stoplist.listing(conn)[0]
    assert e["source"] == "erasure" and "9490000000" not in e["normalized"]
    assert stoplist.Stoplist.load(conn).match(make_vac())
    r = legal_requests.get(conn, rid)
    assert r["executed"] == 1 and r["executed_at"]
    assert audit.recent(conn)[0]["action"] == "request.erase"


def test_erase_by_nick(conn, add):
    a = add(contacts={"telegram": "@Lost_Man", "phones": []})
    res = legal_requests.erase_contact(conn, "nick", "lost_man", ADMIN, NOW)
    assert res.vacancies == 1 and row(conn, a)["status"] == "hidden"
    assert "lost_man" not in row(conn, a)["contacts"].lower()


def test_erase_rejects_bad_value(conn):
    with pytest.raises(ValueError):
        legal_requests.erase_contact(conn, "phone", "12", ADMIN, NOW)
    with pytest.raises(ValueError):
        legal_requests.erase_contact(conn, "employer", "ООО", ADMIN, NOW)


def test_erase_without_matches_still_blocks(conn):
    res = legal_requests.erase_contact(conn, "phone", "+79990001122", ADMIN, NOW)
    assert res.vacancies == 0 and len(stoplist.listing(conn)) == 1


def test_csv_export(conn):
    new(conn, name="=HYPERLINK(1)", body="a, \"b\"\nc")
    out = legal_requests.export_csv(conn, now=NOW)
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0][:3] == ["Номер", "Дата", "От кого"]
    assert rows[1][2].startswith("'=")  # защита от формул в таблицах
