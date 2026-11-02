"""Проверочный скрипт не должен зависеть от размера ответа сайта."""
from __future__ import annotations

import re
from pathlib import Path

SMOKE = Path(__file__).resolve().parents[2] / "scripts" / "smoke.sh"


def test_smoke_does_not_pipe_big_bodies_into_grep_q():
    """При pipefail `printf | grep -q` падает на больших ответах (grep закрывает канал раньше), поэтому здесь-строки."""
    text = SMOKE.read_text(encoding="utf-8")
    assert "pipefail" in text
    bad = [line for line in text.splitlines() if re.search(r"printf\s+'%s'.*\|\s*grep\s+-\w*q", line)]
    assert not bad, bad
