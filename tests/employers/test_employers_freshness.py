"""Метки свежести: чистые функции с фиксированным временем."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rabota.employers import freshness

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


@pytest.mark.parametrize("delta,expected", [
    (timedelta(minutes=5), True), (timedelta(hours=23, minutes=59), True), (timedelta(hours=24), False),
    (timedelta(days=3), False), (timedelta(minutes=-3), True), (timedelta(minutes=-30), False),
])
def test_is_new_window(delta, expected):
    assert freshness.is_new((NOW - delta).isoformat(), NOW) is expected


def test_is_new_handles_missing_and_garbage():
    assert freshness.is_new(None, NOW) is False
    assert freshness.is_new("не дата", NOW) is False


def test_updated_label_two_hours():
    assert freshness.updated_label(ago(days=3), ago(hours=2), NOW) == "обновлено 2 часа назад"


def test_updated_label_yesterday_and_date():
    assert freshness.updated_label(ago(days=10), ago(days=1), NOW) == "обновлено вчера"
    assert freshness.updated_label(ago(days=30), ago(days=9), NOW) == "обновлено 20 октября"


def test_updated_label_empty_when_same_as_publication_or_missing():
    published = ago(days=2)
    assert freshness.updated_label(published, published, NOW) == ""
    assert freshness.updated_label(published, (NOW - timedelta(days=2) + timedelta(minutes=30)).isoformat(), NOW) == ""
    assert freshness.updated_label(published, None, NOW) == ""


def row(**kw):
    base = {"published_at": ago(days=5), "last_seen_at": ago(days=5), "origin": "parsed", "moderation_status": "approved",
            "suspicious": 0}
    base.update(kw)
    return base


def test_chips_new_vacancy():
    chips = freshness.chips(row(published_at=ago(hours=3), last_seen_at=ago(hours=3)), NOW)
    assert [c.kind for c in chips] == ["new"]
    assert chips[0].label == "Новая"


def test_chips_updated_not_new():
    chips = freshness.chips(row(last_seen_at=ago(hours=2)), NOW)
    assert [c.kind for c in chips] == ["updated"] and chips[0].label == "обновлено 2 часа назад"


def test_new_vacancy_does_not_also_say_updated():
    chips = freshness.chips(row(published_at=ago(hours=10), last_seen_at=ago(hours=2)), NOW)
    assert [c.kind for c in chips] == ["new"]


def test_chips_checked_only_for_direct_approved():
    direct = row(origin="direct", published_at=ago(days=9), last_seen_at=ago(days=9))
    assert [c.kind for c in freshness.chips(direct, NOW)] == ["checked"]
    assert freshness.chips(row(), NOW) == []  # собранная вакансия проверенной не бывает
    assert freshness.chips({**direct, "moderation_status": "pending"}, NOW) == []
    assert freshness.chips({**direct, "suspicious": 1}, NOW) == []


def test_chips_direct_new_and_checked():
    chips = freshness.chips(row(origin="direct", published_at=ago(hours=1), last_seen_at=ago(hours=1)), NOW)
    assert [c.kind for c in chips] == ["new", "checked"]


def test_chips_falls_back_to_created_at():
    chips = freshness.chips({"created_at": ago(hours=2).replace("T", " ")[:19], "origin": "parsed"}, NOW)
    assert [c.kind for c in chips] == ["new"]


def test_chip_as_dict():
    d = freshness.Chip("new", "Новая", "подсказка").as_dict()
    assert d == {"kind": "new", "label": "Новая", "title": "подсказка"}
