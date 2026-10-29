"""Чистка текста: гомоглифы, эмодзи, хештеги, ссылки, подписи, капс, пробелы, маркеры."""
from rabota.pipeline.clean import clean_text, normalize_caps
from rabota.pipeline.homoglyphs import fix_homoglyphs, has_homoglyphs


def test_homoglyphs_latin_in_cyrillic_word():
    assert fix_homoglyphs("Тpeбуeтся") == "Требуется"


def test_homoglyphs_greek_letters():
    assert fix_homoglyphs("ЛΒИ") == "ЛВИ"
    assert fix_homoglyphs("ДΗР") == "ДНР"


def test_homoglyphs_pure_latin_word_untouched():
    assert fix_homoglyphs("Hello world, Pixel tattoo") == "Hello world, Pixel tattoo"


def test_homoglyphs_zero_between_letters():
    assert fix_homoglyphs("Д0нецк") == "Донецк"


def test_homoglyphs_digits_untouched():
    assert fix_homoglyphs("оплата 5000 руб, график 2/2, 1С") == "оплата 5000 руб, график 2/2, 1С"


def test_has_homoglyphs():
    assert has_homoglyphs("Тpeбуeтся")
    assert not has_homoglyphs("Требуется")


def test_clean_removes_emoji_and_hashtags():
    out = clean_text("🔥 Требуется повар #работа #Донецк 😀")
    assert "🔥" not in out and "#" not in out and "😀" not in out
    assert "Требуется повар" in out


def test_clean_removes_links():
    out = clean_text("Подробности https://t.me/some_channel/123 и www.example.com тут")
    assert "http" not in out and "t.me" not in out and "example.com" not in out


def test_clean_removes_footers():
    out = clean_text("Требуется повар\n\nРазместить вакансию | Подписаться\nРабота в МАХ")
    assert out == "Требуется повар"


def test_clean_line_marks_become_dash():
    out = clean_text("✅ график 2/2\n• оплата 4000\n▪ звоните")
    assert out.split("\n") == ["- график 2/2", "- оплата 4000", "- звоните"]


def test_clean_numbering_kept():
    out = clean_text("1) Повар\n2) Официант")
    assert out.split("\n") == ["1. Повар", "2. Официант"]


def test_clean_whitespace_and_blank_lines():
    out = clean_text("  Требуется   повар  \n\n\n\n\nЗвоните сейчас\u200b ")
    assert out == "Требуется повар\n\nЗвоните сейчас"


def test_clean_dashes_to_hyphen():
    out = clean_text("Оплата 3000 \u2014 5000 \u2013 в день")
    assert "\u2014" not in out and "\u2013" not in out
    assert "3000 - 5000" in out


def test_clean_caps_normalized():
    out = clean_text("СРОЧНО ТРЕБУЕТСЯ ПОВАР В КАФЕ НА ПОСТОЯННУЮ РАБОТУ")
    assert out == "Срочно требуется повар в кафе на постоянную работу"


def test_caps_keep_abbreviations_and_proper_names():
    out = normalize_caps("ТРЕБУЕТСЯ БУХГАЛТЕР В ООО ДОНЕЦК НА ПОЛНЫЙ ДЕНЬ, ЗНАНИЕ 1С")
    assert "ООО" in out and "Донецк" in out and "1С" in out
    assert out.startswith("Требуется бухгалтер")


def test_clean_empty():
    assert clean_text("") == ""
    assert clean_text("  \n  ") == ""


def test_clean_idempotent():
    once = clean_text("🔥 СРОЧНО!!! ✅ Нужен курьер #работа https://x.ru")
    assert clean_text(once) == once
