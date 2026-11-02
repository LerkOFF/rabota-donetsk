"""Ход отклика для показа: отправлен, просмотрен, приглашение или отказ, у каждого шага своя дата."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

# подписи шагов для соискателя и для работодателя
FINAL = {"invited": "Приглашение", "rejected": "Отказ"}


def timeline(row: dict[str, Any], stamp: Callable[[str | None], str], viewer: str) -> dict[str, Any]:
    """Шаги отклика: [{key, label, when, state}], state: done (был), now (текущий), next (ещё нет)."""
    status = row["status"]
    raw = row.get("raw_status") or status
    seen = row.get("viewed_at") or (row.get("status_changed_at") if raw in ("viewed", "invited", "rejected") else None)
    changed = row.get("status_changed_at")
    employer = viewer == "employer"
    steps: list[dict[str, str]] = [
        {"key": "sent", "label": "Получен" if employer else "Отправлен", "when": stamp(row["created_at"]),
         "state": "now" if status == "new" else "done"},
    ]
    if status == "withdrawn":
        steps.append({"key": "withdrawn", "label": "Отозван", "when": stamp(changed), "state": "now"})
    else:
        steps.append({"key": "viewed", "label": "Просмотрен", "when": stamp(seen) if seen else "",
                      "state": "now" if status == "viewed" else ("done" if seen else "next")})
        if status in FINAL:
            steps.append({"key": status, "label": FINAL[status], "when": stamp(changed), "state": "now"})
        else:
            steps.append({"key": "answer", "label": "Ответ работодателя", "when": "", "state": "next"})
    current = next((s for s in steps if s["state"] == "now"), steps[0])
    return {"steps": steps, "status_when": current["when"]}
