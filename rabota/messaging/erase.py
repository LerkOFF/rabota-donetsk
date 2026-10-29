"""Удаление данных переписки при удалении аккаунта. Вызывается из rabota.accounts.service.delete_account.

- отклики, треды, сообщения и блокировки пользователя удаляются (вместе с ними пропадает и его переписка у собеседника);
- жалоба на сообщение остаётся на срок хранения жалоб, но в копии сообщения автор обезличивается;
- свободный текст жалобы удаляется, если жаловался этот пользователь.
"""
from __future__ import annotations

import json
import sqlite3

from rabota.messaging.rules import REPORT_REASONS


def _anonymise_reports_about(conn: sqlite3.Connection, user_id: int) -> None:
    rows = conn.execute(
        "SELECT id, snapshot FROM reports WHERE target_type = 'message' AND json_valid(snapshot)"
        " AND json_extract(snapshot, '$.sender_user_id') = ?",
        (user_id,),
    ).fetchall()
    for row in rows:
        snap = json.loads(row["snapshot"])
        snap["sender_user_id"] = None
        conn.execute("UPDATE reports SET snapshot = ? WHERE id = ?", (json.dumps(snap, ensure_ascii=False), row["id"]))


def _scrub_reports_by(conn: sqlite3.Connection, user_id: int) -> None:
    rows = conn.execute(
        "SELECT id, reason, snapshot FROM reports WHERE reporter_user_id = ? AND target_type = 'message'", (user_id,)
    ).fetchall()
    for row in rows:
        head = row["reason"].split(": ", 1)[0]
        reason = head if head in REPORT_REASONS else "Другое"
        snap = json.loads(row["snapshot"]) if row["snapshot"] else {}
        if snap:
            snap["comment"] = ""
        conn.execute(
            "UPDATE reports SET reason = ?, snapshot = ? WHERE id = ?",
            (reason, json.dumps(snap, ensure_ascii=False) if snap else "", row["id"]),
        )


def erase_user_data(conn: sqlite3.Connection, user_id: int) -> None:
    _anonymise_reports_about(conn, user_id)
    _scrub_reports_by(conn, user_id)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM conversations WHERE employer_user_id = ? OR seeker_user_id = ?", (user_id, user_id)
    )]
    for conv_id in ids:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_blocks WHERE blocker_user_id = ? OR blocked_user_id = ?", (user_id, user_id))
    conn.commit()
