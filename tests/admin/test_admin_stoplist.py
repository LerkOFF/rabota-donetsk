"""Стоп-лист: нормализация, совпадения, фильтр конвейера."""
import pytest

from rabota.admin import stoplist
from rabota.models import Vacancy
from tests.admin.helpers import ADMIN, NOW, make_vac, row


@pytest.mark.parametrize(
    "kind,raw,expected",
    [
        ("phone", "+7 (949) 123-45-67", "9491234567"),
        ("phone", "8 949 123 45 67", "9491234567"),
        ("phone", "9491234567", "9491234567"),
        ("nick", "@Bad_Guy", "bad_guy"),
        ("nick", "https://t.me/Bad_Guy", "bad_guy"),
        ("employer", "  ООО «Ромашка»  ", "ооо ромашка"),
        ("keyword", "Закладки", "закладки"),
        ("keyword", "Ёлка  Ёж", "елка еж"),
    ],
)
def test_normalize(kind, raw, expected):
    assert stoplist.normalize(kind, raw) == expected


@pytest.mark.parametrize("kind,raw", [("phone", "123"), ("phone", "abc"), ("nick", "@a"), ("nick", "a b!"),
                                      ("employer", "ab"), ("keyword", " я "), ("weird", "xxx")])
def test_normalize_rejects_garbage(kind, raw):
    assert stoplist.normalize(kind, raw) is None


def test_add_is_idempotent_and_audited(conn):
    a, created = stoplist.add(conn, "phone", "+7 949 111-22-33", actor=ADMIN, note="спам", now=NOW)
    b, created2 = stoplist.add(conn, "phone", "89491112233", actor=ADMIN, now=NOW)
    assert a == b and created and not created2
    assert len(stoplist.listing(conn)) == 1


def test_add_invalid_raises(conn):
    with pytest.raises(ValueError):
        stoplist.add(conn, "phone", "12", actor=ADMIN)


def test_remove(conn):
    i, _ = stoplist.add(conn, "nick", "@spam_guy", actor=ADMIN)
    stoplist.remove(conn, i, actor=ADMIN)
    assert stoplist.listing(conn) == []


def sl(conn, *entries):
    for kind, value in entries:
        stoplist.add(conn, kind, value, actor=ADMIN)
    return stoplist.Stoplist.load(conn)


def test_match_phone_any_format(conn):
    s = sl(conn, ("phone", "+7 949 000-00-00"))
    assert s.match(make_vac(contacts={"phones": ["8 (949) 000 00 00"]}))
    assert s.match(make_vac(contacts={"phones": ["+79490000001", "+79490000000"]}))
    assert not s.match(make_vac(contacts={"phones": ["+79490000001"]}))


def test_match_nick_in_telegram_and_max(conn):
    s = sl(conn, ("nick", "@bad_hr"))
    assert s.match(make_vac(contacts={"telegram": "@Bad_HR"}))
    assert s.match(make_vac(contacts={"max": "bad_hr"}))
    assert not s.match(make_vac(contacts={"telegram": "@good_hr"}))


def test_match_employer_exact_normalized(conn):
    s = sl(conn, ("employer", "ООО Тёмные Дела"))
    assert s.match(make_vac(employer='ооо "темные дела"'))
    assert not s.match(make_vac(employer="Тёмные Дела Плюс"))


def test_match_keyword_in_text_fields(conn):
    s = sl(conn, ("keyword", "закладк"))
    assert s.match(make_vac(summary="Работа курьером, закладки по району"))
    assert s.match(make_vac(profession="Курьер-закладчик", title_raw="ЗАКЛАДКИ"))
    assert not s.match(make_vac(summary="Работа бариста"))


def test_match_returns_kind_and_entry(conn):
    s = sl(conn, ("keyword", "залог"))
    m = s.match(make_vac(summary="нужен залог за форму"))
    assert m.kind == "keyword" and m.entry_id


def test_match_accepts_dict_rows_with_json_contacts(conn, add):
    s = sl(conn, ("phone", "+79490000000"))
    vid = add()
    assert s.match(dict(row(conn, vid)))


def test_drop_stoplisted_counts_hits(conn):
    stoplist.add(conn, "phone", "+79490000000", actor=ADMIN)
    kept = stoplist.drop_stoplisted(conn, [make_vac(), make_vac(contacts={"phones": ["+79491112233"]})])
    assert len(kept) == 1 and kept[0].contacts["phones"] == ["+79491112233"]
    e = stoplist.listing(conn)[0]
    assert e["hits"] == 1 and e["last_hit_at"]


def test_drop_with_empty_stoplist_is_noop(conn):
    vs = [make_vac(), make_vac(profession="повар")]
    assert stoplist.drop_stoplisted(conn, vs) == vs


def test_hashed_entry_matches_without_storing_value(conn):
    stoplist.add(conn, "phone", "+7 949 555-66-77", actor=ADMIN, hashed=True, source="erasure")
    e = stoplist.listing(conn)[0]
    assert "5556677" not in e["normalized"] and "5556677" not in e["value"]
    assert e["value"].startswith("+7949")  # маска: начало и конец
    s = stoplist.Stoplist.load(conn)
    assert s.match(make_vac(contacts={"phones": ["89495556677"]}))
    assert not s.match(make_vac(contacts={"phones": ["89495556678"]}))


def test_apply_to_existing_hides_active_matches(conn, add):
    a = add(contacts={"phones": ["+79490000000"]})
    b = add(contacts={"phones": ["+79491112233"]})
    i, _ = stoplist.add(conn, "phone", "+79490000000", actor=ADMIN)
    n = stoplist.apply_to_existing(conn, actor=ADMIN, entry_id=i, now=NOW)
    assert n == 1
    assert row(conn, a)["status"] == "hidden" and "стоп-лист" in row(conn, a)["moderation_note"].lower()
    assert row(conn, b)["status"] == "active"


def test_vacancy_dataclass_type():
    assert isinstance(make_vac(), Vacancy)
