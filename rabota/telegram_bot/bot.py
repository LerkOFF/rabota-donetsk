"""Команды бота: /start (с кодом привязки и без), /list, /stop (с подтверждением), /help.

`plan(conn, update, ...)` разбирает одно обновление и возвращает список ответов, ничего не отправляя: так логика
проверяется без сети. `process` отправляет ответы через клиент Bot API. Бот работает только в личных чатах.
"""
from __future__ import annotations

import html
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rabota.alerts import service
from rabota.telegram_bot.api import BotApi, BotApiError

log = logging.getLogger("rabota.telegram_bot")

STOP_YES = "stop:yes"
STOP_NO = "stop:no"


@dataclass
class Reply:
    chat_id: int
    text: str = ""
    markup: dict[str, Any] | None = None
    callback_id: str = ""        # на какое нажатие кнопки отвечаем
    callback_text: str = ""
    edit_message_id: int = 0     # заменить текст прежнего сообщения (без кнопок), а не слать новое
    extra: dict[str, Any] = field(default_factory=dict)


def _e(text: Any) -> str:
    return html.escape(str(text), quote=False)


def welcome(site_url: str) -> str:
    return (
        "Привет! Я присылаю новые вакансии Донецка и ДНР, как только они появляются на сайте.\n\n"
        "Как подписаться:\n"
        f"1. Откройте <a href=\"{site_url}/vacancies\">поиск на сайте</a> и задайте профессию или фильтры.\n"
        "2. Нажмите «Подписаться на такие вакансии» и выберите Telegram.\n"
        "3. Вернитесь сюда по кнопке: подписка подключится сама.\n\n"
        "Что я умею: слать новые вакансии сразу или раз в день, показывать ваши подписки (/list) "
        "и отключать их (/stop). Справка: /help."
    )


def help_text(site_url: str) -> str:
    return (
        "Как это работает.\n\n"
        "Вы сохраняете поиск на сайте, например «бариста в Донецке», и я пишу, когда появляются новые подходящие вакансии. "
        "Одну и ту же вакансию дважды не присылаю. Частота у каждой подписки своя: сразу или раз в день, её можно поменять на сайте.\n\n"
        "/list - мои подписки\n"
        "/stop - отключить все подписки в этом чате\n"
        "/help - эта справка\n\n"
        f"Подписки, почта и настройки: <a href=\"{site_url}/alerts\">{_e(site_url.split('//', 1)[-1])}/alerts</a>"
    )


def link_ok(sub: sqlite3.Row) -> str:
    return (
        f"Готово! Подписка «{_e(sub['title'])}» подключена ({service.FREQUENCY_LABELS[sub['frequency']]}). "
        "Новые вакансии пришлю сюда. Мои подписки: /list, отключить: /stop."
    )


def list_text(subs: list[sqlite3.Row], site_url: str) -> str:
    if not subs:
        return (
            "Подписок пока нет. Задайте поиск на сайте и нажмите «Подписаться на такие вакансии»: "
            f"<a href=\"{site_url}/vacancies\">открыть вакансии</a>."
        )
    lines = [f"Ваши подписки ({len(subs)}):"]
    for i, sub in enumerate(subs, 1):
        mark = "" if sub["status"] == "active" else " (на паузе)"
        lines.append(f"{i}. {_e(sub['title'])} - {service.FREQUENCY_LABELS[sub['frequency']]}{mark}")
    lines.append("")
    lines.append(f"Менять и удалять по одной можно на сайте: <a href=\"{site_url}/alerts\">мои подписки</a>. Отключить всё: /stop.")
    return "\n".join(lines)


def _command(text: str) -> tuple[str, str]:
    """"/start@bot abc" -> ("/start", "abc")."""
    head, _, rest = (text or "").strip().partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def plan(conn: sqlite3.Connection, update: dict[str, Any], *, site_url: str, now: datetime) -> list[Reply]:
    """Ответы на одно обновление. Чужие типы обновлений и групповые чаты игнорируются."""
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat") or {}
        status = (member.get("new_chat_member") or {}).get("status")
        if chat.get("type") == "private" and status in ("kicked", "left") and chat.get("id"):
            service.stop_chat(conn, int(chat["id"]), now, "бот заблокирован")
        return []
    callback = update.get("callback_query")
    if callback:
        return _on_callback(conn, callback, now)
    message = update.get("message")
    if not message or (message.get("chat") or {}).get("type") != "private":
        return []
    chat_id = int(message["chat"]["id"])
    command, arg = _command(message.get("text") or "")
    if command == "/start":
        return _start(conn, chat_id, arg, site_url, now)
    if command == "/list":
        return [Reply(chat_id, list_text(service.subs_for_chat(conn, chat_id), site_url))]
    if command == "/stop":
        subs = service.subs_for_chat(conn, chat_id)
        if not subs:
            return [Reply(chat_id, "Отключать нечего: подписок в этом чате нет. Новые можно оформить на сайте.")]
        word = "подписку" if len(subs) == 1 else f"подписки ({len(subs)})"
        markup = {"inline_keyboard": [[
            {"text": "Да, отключить", "callback_data": STOP_YES}, {"text": "Нет, оставить", "callback_data": STOP_NO},
        ]]}
        return [Reply(chat_id, f"Отключить {word} в этом чате? Вакансии приходить перестанут.", markup)]
    if command == "/help":
        return [Reply(chat_id, help_text(site_url))]
    if command.startswith("/"):
        return [Reply(chat_id, "Такой команды нет. Вот что я умею: /list, /stop, /help.")]
    return [Reply(chat_id, "Я отвечаю только на команды: /list, /stop, /help. Подписаться на вакансии можно на сайте.")]


def _start(conn: sqlite3.Connection, chat_id: int, code: str, site_url: str, now: datetime) -> list[Reply]:
    if not code:
        return [Reply(chat_id, welcome(site_url))]
    status, sub = service.bind_telegram(conn, code, chat_id, now)
    if status == "ok" and sub is not None:
        return [Reply(chat_id, link_ok(sub))]
    if status == "limit":
        return [Reply(chat_id, "В этом чате уже много подписок. Отключите лишние (/stop) или удалите их на сайте и попробуйте снова.")]
    return [Reply(
        chat_id,
        "Эта ссылка уже не действует: код использован или устарел. "
        f"Откройте подписку на сайте ещё раз: <a href=\"{site_url}/alerts\">{_e(site_url.split('//', 1)[-1])}/alerts</a>.",
    )]


def _on_callback(conn: sqlite3.Connection, callback: dict[str, Any], now: datetime) -> list[Reply]:
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not chat.get("id"):
        return []
    chat_id = int(chat["id"])
    cid = str(callback.get("id") or "")
    mid = int(message.get("message_id") or 0)
    data = callback.get("data")
    if data == STOP_YES:
        n = service.stop_chat(conn, chat_id, now, "отключено командой /stop")
        text = "Готово, подписки отключены. Если захотите вернуться, оформите новые на сайте." if n else "Подписок уже нет."
        return [Reply(chat_id, text, callback_id=cid, callback_text="Отключено", edit_message_id=mid)]
    if data == STOP_NO:
        return [Reply(chat_id, "Хорошо, ничего не меняю.", callback_id=cid, edit_message_id=mid)]
    return [Reply(chat_id, callback_id=cid)]


async def send_replies(api: BotApi, replies: list[Reply]) -> None:
    for r in replies:
        try:
            if r.callback_id:
                await api.answer_callback_query(r.callback_id, r.callback_text)
            if not r.text:
                continue
            if r.edit_message_id:
                await api.edit_message_text(r.chat_id, r.edit_message_id, r.text)
            else:
                await api.send_message(r.chat_id, r.text, reply_markup=r.markup)
        except BotApiError as exc:
            log.warning("ответ боту не доставлен: %s", exc)
