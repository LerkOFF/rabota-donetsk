"""Уведомления в Telegram о сбоях приёма данных.

    python -m rabota.ops.alerts [--dry-run]

Запускается таймером раз в 5 минут. Берёт отчёт из rabota.ops.deep, сравнивает с прошлым запуском
(файл состояния рядом с базой) и пишет в бот только об изменениях: новая проблема, восстановление,
напоминание раз в 6 часов, если проблема не ушла. Без ALERT_TG_BOT_TOKEN и ALERT_TG_CHAT_ID молча ничего не делает.
Telegram с сервера может быть закрыт, поэтому запрос идёт через TELEGRAM_PROXY, если он задан.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from rabota import config
from rabota.ops import deep
from rabota.ops.logs import redact

log = logging.getLogger("rabota.alerts")

REMIND_EVERY = timedelta(hours=6)
MAX_LINES = 8
API = "https://api.telegram.org/bot{token}/sendMessage"

Sender = Callable[[str], bool]


def problems_from(report: Mapping[str, Any]) -> dict[str, str]:
    """Ключ проблемы -> короткое описание. Группа источников считается проблемой, когда она не в порядке целиком."""
    out: dict[str, str] = {}
    if report["db"] != "ok":
        out["db"] = "база данных недоступна"
    worker = report["worker"]
    if worker == "missing":
        out["worker"] = "воркер не запущен (нет сердцебиения)"
    elif worker != "ok":
        out["worker"] = "воркер молчит"
    for name, info in report["groups"].items():
        if info["state"] == "ok":
            continue
        label = deep.SOURCE_GROUPS.get(name, name)
        head = f"{label}: просрочено {info['bad']} из {info['total']}"
        if info["state"] == "down":
            out[f"group:{name}"] = head
        else:
            out[f"part:{name}"] = head + " (частично)"
    return out


def details(report: Mapping[str, Any], keys: list[str]) -> list[str]:
    lines: list[str] = []
    for key in keys:
        if ":" not in key:
            continue
        info = report["groups"].get(key.split(":", 1)[1])
        for item in (info or {}).get("problems", [])[:MAX_LINES]:
            err = f" - {item['last_error']}" if item.get("last_error") else ""
            lines.append(f"  {item['label']}{err}"[:200])
    return lines


def compose(report: Mapping[str, Any], now_problems: dict[str, str], prev: dict[str, str], remind: bool) -> str | None:
    new = [k for k in now_problems if k not in prev]
    fixed = [k for k in prev if k not in now_problems]
    if not new and not fixed and not (remind and now_problems):
        return None
    lines: list[str] = []
    if new:
        lines.append("Сбой на работа-донецк.рф:")
        lines += [f"- {now_problems[k]}" for k in new]
        lines += details(report, new)[:MAX_LINES]
    if fixed:
        lines.append("Восстановилось:")
        lines += [f"- {prev[k]}" for k in fixed]
    if remind and not new and now_problems:
        lines.append("Проблемы остаются:")
        lines += [f"- {text}" for text in now_problems.values()]
    return redact("\n".join(lines))


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, problems: dict[str, str], sent_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"problems": problems, "sent_at": sent_at.isoformat()}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def telegram_sender(token: str, chat_id: str, proxy: str = "") -> Sender:
    def send(text: str) -> bool:
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), proxy=proxy or None) as client:
                resp = client.post(API.format(token=token), json={"chat_id": chat_id, "text": text,
                                                                  "disable_web_page_preview": True})
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("уведомление не отправлено: %s", type(exc).__name__)
            return False

    return send


def run(
    report: Mapping[str, Any],
    state_path: Path,
    send: Sender | None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> str | None:
    """Вернуть отправленный текст или None. Состояние обновляется только после успешной отправки."""
    now = now or datetime.now(UTC)
    state = load_state(state_path)
    prev: dict[str, str] = state.get("problems") or {}
    try:
        last = datetime.fromisoformat(state["sent_at"]) if state.get("sent_at") else None
    except ValueError:
        last = None
    current = problems_from(report)
    remind = last is None or now - last >= REMIND_EVERY
    text = compose(report, current, prev, remind)
    if text is None:
        if not current and prev:
            save_state(state_path, current, now)
        return None
    if dry_run:
        return text
    if send is None or not send(text):
        return None
    save_state(state_path, current, now)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Уведомления о сбоях источников в Telegram")
    parser.add_argument("--dry-run", action="store_true", help="напечатать текст, ничего не отправлять и не запоминать")
    args = parser.parse_args(argv)
    token, chat = config.get("ALERT_TG_BOT_TOKEN").strip(), config.get("ALERT_TG_CHAT_ID").strip()
    if not (token and chat) and not args.dry_run:
        print("уведомления выключены: нет ALERT_TG_BOT_TOKEN и ALERT_TG_CHAT_ID")
        return 0
    report = deep.build_report(config.DB_PATH)
    state_path = Path(config.get("ALERT_STATE_PATH") or config.DB_PATH.parent / "alert-state.json")
    sender = telegram_sender(token, chat, config.telegram_proxy()) if token and chat else None
    text = run(report, state_path, sender, dry_run=args.dry_run)
    if text:
        print(text if args.dry_run else "уведомление отправлено")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
