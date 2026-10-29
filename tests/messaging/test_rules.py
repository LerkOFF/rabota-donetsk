"""Очистка и проверка текста сообщений."""
import pytest

from rabota.messaging import rules


@pytest.mark.parametrize("raw,expected", [
    ("  привет  ", "привет"),
    ("a\r\nb\rc", "a\nb\nc"),
    ("a\x00b\x07c", "abc"),
    ("a   b\t\tc", "a b c"),
    ("a\n\n\n\n\nb", "a\n\nb"),
    ("строка   \nвторая  ", "строка\nвторая"),
    ("a\u200bb\u202ec\ufeffd", "abcd"),
    ("", ""),
])
def test_clean_text(raw, expected):
    assert rules.clean_text(raw) == expected


def test_clean_text_keeps_html_as_text():
    assert rules.clean_text("<b>привет</b> <script>x</script>") == "<b>привет</b> <script>x</script>"


def test_check_text_empty_and_blank():
    with pytest.raises(rules.MessagingError) as e:
        rules.check_text("  \n ", rules.MAX_MESSAGE)
    assert e.value.code == "empty"


def test_check_text_too_long():
    with pytest.raises(rules.MessagingError) as e:
        rules.check_text("а" * (rules.MAX_MESSAGE + 1), rules.MAX_MESSAGE)
    assert e.value.code == "too_long"
    assert rules.check_text("а" * rules.MAX_MESSAGE, rules.MAX_MESSAGE)


def test_check_text_allows_empty_when_optional():
    assert rules.check_text("   ", rules.MAX_COVER, required=False) == ""


def test_too_many_links_rejected():
    ok = "Смотрите http://a.ru и https://b.ru"
    assert rules.check_text(ok, rules.MAX_MESSAGE) == ok
    with pytest.raises(rules.MessagingError) as e:
        rules.check_text("http://a.ru https://b.ru www.c.ru", rules.MAX_MESSAGE)
    assert e.value.code == "too_many_links"


def test_status_labels_cover_every_status():
    for status in (*rules.APP_STATUSES, "withdrawn"):
        assert rules.STATUS_LABELS[status]
    assert set(rules.EMPLOYER_SET_STATUSES) <= set(rules.APP_STATUSES)
    assert "new" not in rules.EMPLOYER_SET_STATUSES
