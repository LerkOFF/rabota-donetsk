"""Отрисовщик Markdown для юридических текстов: разметка, экранирование, переменные, ссылки."""
from rabota.legal import render_markdown


def test_headings_paragraphs_lists_bold_links():
    md = (
        "# Название\n\nПервый абзац\nвторая строка с **жирным**.\n\n"
        "## Раздел один\n\n- пункт а\n- пункт б\n  продолжение\n\n"
        "1. раз\n2. два\n\n### Подраздел\n\n[Политика](/legal/privacy) и [сайт](https://example.com/a?b=1&c=2)\n"
    )
    r = render_markdown(md, {})
    assert r.title == "Название"
    assert "<h1" not in r.html
    assert '<p>Первый абзац вторая строка с <strong>жирным</strong>.</p>' in r.html
    assert '<h2 class="h3" id="s1">Раздел один</h2>' in r.html
    assert "<li>пункт б продолжение</li>" in r.html
    assert '<ol class="about__list legal__ol"><li>раз</li><li>два</li></ol>' in r.html
    assert "<h3>Подраздел</h3>" in r.html
    assert '<a href="/legal/privacy">Политика</a>' in r.html
    assert 'href="https://example.com/a?b=1&amp;c=2" rel="noopener noreferrer"' in r.html


def test_toc_lists_only_second_level_headings_with_matching_ids():
    r = render_markdown("# T\n\n## Один\n\ntext\n\n### Вложенный\n\n## Два\n", {})
    assert r.toc == [("s1", "Один"), ("s2", "Два")]
    assert 'id="s1"' in r.html and 'id="s2"' in r.html
    assert r.html.count("id=") == 2


def test_id_prefix_is_applied():
    r = render_markdown("## Один\n", {}, id_prefix="consent-")
    assert 'id="consent-1"' in r.html


def test_quote_becomes_notice():
    r = render_markdown("> **Черновик, требует проверки юристом.**\n", {})
    assert '<p class="notice notice--warn"><span><strong>Черновик, требует проверки юристом.</strong></span></p>' in r.html


def test_html_is_escaped():
    r = render_markdown("# <b>x</b>\n\n<script>alert(1)</script> & \"q\"\n\n- <img src=x onerror=1>\n", {})
    assert "<script" not in r.html and "<img" not in r.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;" in r.html
    assert "&lt;img src=x onerror=1&gt;" in r.html
    assert r.title == "&lt;b&gt;x&lt;/b&gt;"


def test_unsafe_link_schemes_are_dropped():
    r = render_markdown(
        "[a](javascript:alert(1)) [b](//evil.example/x) [c](data:text/html;base64,AA) [d](mailto:a@b.test) [e](#s1)\n", {}
    )
    assert "javascript:" not in r.html
    assert "evil.example" not in r.html
    assert "data:" not in r.html
    assert '<a href="mailto:a@b.test">d</a>' in r.html
    assert '<a href="#s1">e</a>' in r.html
    assert ">a<" not in r.html or "<a href" in r.html  # подпись остаётся обычным текстом


def test_link_cannot_break_out_of_attribute():
    r = render_markdown('[x](/a"onmouseover="alert(1))\n', {})
    assert '" onmouseover' not in r.html
    assert 'onmouseover="' not in r.html


def test_variables_are_substituted_and_escaped():
    r = render_markdown("Оператор {{operator_name}} на {{site_url}}\n", {"operator_name": "<i>ООО \"Х\"</i>", "site_url": "https://a.test"})
    assert "&lt;i&gt;ООО &quot;Х&quot;&lt;/i&gt;" in r.html
    assert "<i>" not in r.html
    assert "https://a.test" in r.html
    assert "{{" not in r.html


def test_variable_value_is_not_interpreted_as_markdown():
    r = render_markdown("Имя {{operator_name}}\n", {"operator_name": "**x** [y](https://evil.test)"})
    assert "<strong>" not in r.html
    assert "<a " not in r.html
    assert "**x** [y](https://evil.test)" in r.html


def test_variable_in_link_target_and_label():
    r = render_markdown("[{{operator_email}}](mailto:{{operator_email}})\n", {"operator_email": "a@b.test"})
    assert '<a href="mailto:a@b.test">a@b.test</a>' in r.html


def test_unknown_variable_left_as_text():
    r = render_markdown("Привет {{nope}}\n", {})
    assert "{{nope}}" in r.html


def test_forged_token_characters_are_removed():
    r = render_markdown("текст 0 {{operator_name}}\n", {"operator_name": "X"})
    assert "" not in r.html and "" not in r.html
    assert r.html.count("X") == 1
