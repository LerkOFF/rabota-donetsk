"""Перетаскивание блоков: скрипт подключён на всех страницах админки, разметка есть у разделов."""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "rabota" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parents[2] / "rabota" / "web" / "templates" / "admin"


def test_base_template_loads_dnd_assets():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "admin-dnd.js" in base
    assert "admin-dnd.css" in base


def test_dnd_script_contract():
    js = (STATIC / "admin-dnd.js").read_text(encoding="utf-8")
    for token in ("data-sortable", "data-key", "localStorage", "touchstart", "Сбросить порядок", "altKey"):
        assert token in js


def test_every_admin_section_has_sortable_blocks():
    for name in ("pulse.html", "system.html", "settings.html", "vacancies.html", "vacancy.html"):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "data-sortable=" in html, name
        assert "data-key=" in html, name
