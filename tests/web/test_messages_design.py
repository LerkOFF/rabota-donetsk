"""Страницы откликов и переписки: только токены, без встроенных стилей, подписи у полей, нет служебных слов."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "rabota" / "web"
MESSAGING = Path(__file__).resolve().parents[2] / "rabota" / "messaging"


def test_messages_css_uses_only_tokens():
    css = (WEB / "static" / "messages.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not re.search(r"\brgba?\(", css)
    assert not re.search(r"font-family\s*:(?!\s*var\()", css)
    assert "@font-face" not in css


def test_message_templates_have_no_inline_styles_or_handlers():
    for path in list((WEB / "templates" / "messages").glob("*.html")) + list((WEB / "templates" / "email").glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle=", text), path.name
        assert "onclick=" not in text and "javascript:" not in text, path.name
        for tag in re.findall(r"<img\b[^>]*>", text):
            assert "alt=" in tag, path.name


def test_every_field_has_an_id_for_its_label():
    for path in (WEB / "templates" / "messages").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", text):
            if 'type="hidden"' in tag or 'name="website"' in tag or 'type="checkbox"' in tag:
                continue
            assert 'id="' in tag, f"{path.name}: поле без id: {tag[:80]}"


def test_no_internal_words_or_long_dashes_in_messaging_files():
    forbidden = re.compile("|".join(("cla" + "ude", "cod" + "ex", "аген" + "т", "скил" + "л", "vau" + "lt", "obsi" + "dian",
                                     "/Us" + "ers/", "adven" + "tcher", "joul" + "erk")), re.IGNORECASE)
    files = [*MESSAGING.rglob("*.py"), *(WEB / "templates" / "messages").glob("*.html"),
             *(WEB / "templates" / "email").glob("*.txt"), WEB / "static" / "messages.css", WEB / "static" / "messages.js",
             WEB / "messages_routes.py", Path(__file__).resolve().parents[2] / "docs" / "MESSAGING.md"]
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), path.name
        assert chr(0x2014) not in text and chr(0x2013) not in text, path.name
