"""Распознавание афиш требует tesseract с русским языком: установка и документация это называют."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_bootstrap_installs_tesseract_with_russian_language():
    text = (ROOT / "scripts" / "bootstrap-server.sh").read_text(encoding="utf-8")
    assert "tesseract-ocr tesseract-ocr-rus" in text
    assert "--list-langs" in text


def test_deploy_doc_mentions_tesseract_rus_and_the_new_commands():
    text = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    assert "tesseract-ocr-rus" in text and "reparse-posters" in text and "quality-sweep" in text
