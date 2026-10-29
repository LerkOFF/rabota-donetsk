"""Проверки оформления без браузера: контраст токенов, только токены в компонентах, чистота текстов."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "rabota" / "web"
TOKENS = (WEB / "static" / "tokens.css").read_text(encoding="utf-8")


def parse_block(css: str, header: str) -> dict[str, str]:
    start = css.index(header)
    body = css[css.index("{", start) + 1 :]
    depth, out = 1, []
    for ch in body:
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            break
        out.append(ch)
    return dict(re.findall(r"(--c-[\w-]+):\s*([^;]+);", "".join(out)))


def light() -> dict[str, str]:
    return parse_block(TOKENS, ":root {")


def dark() -> dict[str, str]:
    return parse_block(TOKENS, ':root[data-theme="dark"] {')


def dark_media() -> dict[str, str]:
    return parse_block(TOKENS, ':root:not([data-theme="light"]) {')


def rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), value
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def luminance(c: tuple[float, float, float]) -> float:
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(rgb(a)), luminance(rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("--c-ink", "--c-bg"), ("--c-ink", "--c-surface"), ("--c-ink", "--c-surface-2"), ("--c-ink", "--c-accent-soft"),
    ("--c-ink-2", "--c-bg"), ("--c-ink-2", "--c-surface"), ("--c-ink-2", "--c-surface-2"),
    ("--c-on-accent", "--c-accent"), ("--c-on-accent", "--c-accent-hover"),
    ("--c-accent-ink", "--c-bg"), ("--c-accent-ink", "--c-surface"), ("--c-accent-ink", "--c-accent-soft"),
    ("--c-accent-hover", "--c-bg"), ("--c-accent-hover", "--c-surface"),
    ("--c-on-pay", "--c-pay"),
    ("--c-danger", "--c-danger-soft"), ("--c-danger", "--c-surface"), ("--c-danger", "--c-bg"),
    ("--c-ink", "--c-danger-soft"),
]
UI_PAIRS = [  # границы полей и фокус: не меньше 3:1
    ("--c-line-strong", "--c-surface"), ("--c-line-strong", "--c-bg"),
    ("--c-focus", "--c-bg"), ("--c-focus", "--c-surface"),
]


@pytest.mark.parametrize("theme", [light, dark], ids=["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_contrast_wcag_aa(theme, fg, bg):
    t = theme()
    assert contrast(t[fg], t[bg]) >= 4.5, f"{fg} на {bg}: {contrast(t[fg], t[bg]):.2f}"


@pytest.mark.parametrize("theme", [light, dark], ids=["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), UI_PAIRS)
def test_interface_contrast_at_least_3(theme, fg, bg):
    t = theme()
    assert contrast(t[fg], t[bg]) >= 3.0, f"{fg} на {bg}: {contrast(t[fg], t[bg]):.2f}"


def test_dark_theme_is_defined_the_same_for_system_and_switch():
    assert dark() == dark_media()
    assert set(light()) == set(dark())


def test_components_use_only_tokens():
    css = (WEB / "static" / "site.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "цвета в компонентах только через токены"
    assert not re.search(r"\brgba?\(", css)
    assert not re.search(r"font-family\s*:(?!\s*var\()", css)
    assert "@font-face" not in css


def test_fonts_swap_and_cyrillic_present():
    assert TOKENS.count("font-display: swap") >= 3
    assert "U+0400-045F" in TOKENS
    for name in ("onest-cyrillic", "onest-latin", "onest-latin-ext"):
        assert (WEB / "static" / "fonts" / f"{name}.woff2").stat().st_size > 5000


def test_templates_have_no_inline_styles_and_images_have_alt():
    for path in (WEB / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle=", text), f"{path.name}: стили только в css (строгая политика CSP)"
        for tag in re.findall(r"<img\b[^>]*>", text):
            assert "alt=" in tag, f"{path.name}: у картинки нет alt"
        assert "onclick=" not in text and "javascript:" not in text


def test_html_forms_have_labels_for_every_field():
    for name in ("post.html", "vacancies.html", "vacancy.html", "index.html"):
        text = (WEB / "templates" / name).read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", text):
            if 'type="hidden"' in tag or 'type="submit"' in tag or 'name="website"' in tag:
                continue
            if 'type="checkbox"' in tag:
                continue  # чекбокс внутри <label>
            assert 'id="' in tag, f"{name}: поле без id: {tag[:70]}"


FORBIDDEN = [
    "cla" + "ude", "cur" + "sor(?!\\s*:)", "cod" + "ex", "аген" + "т", "скил" + "л", "vau" + "lt", "obsi" + "dian",
    "/Us" + "ers/", "adven" + "tcher", "joul" + "erk",
]


def project_text_files():
    for base in (WEB, ROOT / "tests" / "web", ROOT / "scripts"):
        for path in base.rglob("*"):
            if path.suffix in {".py", ".html", ".css", ".js", ".svg"} and "__pycache__" not in path.parts:
                if path.name == "icons.svg":
                    continue
                yield path


def test_no_internal_kitchen_words_or_long_dashes():
    pattern = re.compile("|".join(w if "(?!" in w else re.escape(w) for w in FORBIDDEN), re.IGNORECASE)
    for path in project_text_files():
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.relative_to(ROOT)}: служебное слово в публичном репозитории"
        assert chr(0x2014) not in text and chr(0x2013) not in text, f"{path.relative_to(ROOT)}: длинное тире"
