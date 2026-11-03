"""Уведомление владельцу в Telegram: вакансия работодателя пришла на модерацию.

Вызывается там, где вакансия остаётся без публикации и ждёт решения: новая вакансия после автомодерации
(`automod.moderate_new`) и правка опубликованной или отклонённой вакансии, ушедшая на повторную проверку
(`cabinet.update_vacancy`).

Настройки в `.env`:

- `MODERATION_TG_CHAT_ID`: кому писать (числовой id чата). Пусто: уведомления выключены.
- `MODERATION_TG_BOT_TOKEN`: токен бота. Пусто: берётся `ALERT_TG_BOT_TOKEN`, затем `TG_BOT_TOKEN`.
  Получатель должен хотя бы раз нажать Start у этого бота, иначе Telegram не даст ему написать.

Отправка идёт в отдельном потоке и не влияет на ответ сайта: любая ошибка только пишется в журнал.
Запрос к Telegram идёт через `TELEGRAM_PROXY`, если он задан.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable

import httpx

from rabota import config

log = logging.getLogger("rabota.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# "new": новая вакансия, "edit": правка опубликованной ушла на повторную проверку, "resubmit": отклонённую отправили снова
TITLES = {
    "new": "На модерацию поступила вакансия",
    "edit": "Вакансию отредактировали, нужна повторная проверка",
    "resubmit": "Отклонённую вакансию отправили на проверку снова",
}
PERIODS = {"месяц": "в месяц", "смена": "за смену", "день": "в день", "час": "в час", "задача": "за задачу"}

Sender = Callable[[str, str, str], bool]  # (токен, чат, текст) -> доставлено ли


def settings() -> tuple[str, str]:
    """(токен, чат). Пустая строка в любом поле: уведомления выключены."""
    chat = config.get("MODERATION_TG_CHAT_ID").strip()
    token = (config.get("MODERATION_TG_BOT_TOKEN") or config.get("ALERT_TG_BOT_TOKEN") or config.get("TG_BOT_TOKEN")).strip()
    return token, chat


def _pay(row: sqlite3.Row) -> str:
    low, high = row["pay_min"], row["pay_max"]
    if not low and not high:
        return ""
    if low and high and low != high:
        amount = f"{low}-{high}"
    else:
        amount = str(high or low)
    period = PERIODS.get(str(row["pay_period"] or ""), "")
    return f"{amount} ₽ {period}".strip()


def compose(row: sqlite3.Row, kind: str, site_url: str) -> str:
    """Текст сообщения без разметки (обычный текст, чужие символы Telegram не разбирает)."""
    lines = [f"{TITLES.get(kind, TITLES['new'])} #{row['id']}"]
    head = ", ".join(x for x in (row["profession"] or row["title_raw"], row["employer"]) if x)
    if head:
        lines.append(head[:200])
    place = ", ".join(x for x in (row["city"], row["district"]) if x)
    pay = _pay(row)
    details = " | ".join(x for x in (place, pay) if x)
    if details:
        lines.append(details[:200])
    from rabota.employers import automod  # поздний импорт: automod сам вызывает это уведомление

    labels = automod.flag_labels(row["automod_flags"])
    if labels:
        lines.append("Пометки: " + "; ".join(labels))
    lines.append(f"{site_url}/admin/vacancies/{row['id']}")
    return "\n".join(lines)


def _post(token: str, chat: str, text: str) -> bool:
    try:
        with httpx.Client(timeout=TIMEOUT, proxy=config.telegram_proxy() or None) as client:
            resp = client.post(API.format(token=token), json={
                "chat_id": chat, "text": text, "disable_web_page_preview": True})
    except httpx.HTTPError as exc:
        log.warning("уведомление о модерации не отправлено: %s", type(exc).__name__)
        return False
    if resp.status_code != 200:
        log.warning("уведомление о модерации не отправлено: Telegram ответил %s", resp.status_code)
        return False
    return True


def _deliver(sender: Sender, token: str, chat: str, text: str) -> None:
    try:
        sender(token, chat, text)
    except Exception:
        log.exception("уведомление о модерации: сбой отправки")


# Подменяется в тестах: тогда отправка идёт синхронно.
sender: Sender = _post
synchronous = False


def moderation_pending(conn: sqlite3.Connection, vacancy_id: int, kind: str = "new") -> bool:
    """Сообщить владельцу, что вакансия ждёт модерации. True: отправка запущена (доставка не гарантируется).

    Ничего не делает, если уведомления не настроены или вакансия не в статусе `pending`
    (отклонена автомодерацией или опубликована сразу)."""
    token, chat = settings()
    if not (token and chat):
        return False
    try:
        row = conn.execute(
            "SELECT id, profession, title_raw, employer, city, district, pay_min, pay_max, pay_period, automod_flags,"
            " moderation_status FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
        if row is None or row["moderation_status"] != "pending":
            return False
        text = compose(row, kind, config.site_url())
    except sqlite3.Error:
        log.exception("уведомление о модерации: не прочитана вакансия %s", vacancy_id)
        return False
    if synchronous:
        _deliver(sender, token, chat, text)
    else:
        threading.Thread(target=_deliver, args=(sender, token, chat, text), name="notify-moderation", daemon=True).start()
    return True


__all__ = ["compose", "moderation_pending", "settings"]
