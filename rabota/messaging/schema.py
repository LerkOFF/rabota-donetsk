"""Колонки и индексы откликов и переписки поверх базовой схемы. Вызывается из rabota.db.connect, повтор безопасен."""
from __future__ import annotations

import sqlite3

# (таблица, колонка, описание): добавляются только если колонки ещё нет
COLUMNS = (
    # контакты соискателя на момент отклика (имя и телефон из профиля, если он разрешил показать телефон)
    ("applications", "contact_name", "TEXT"),
    ("applications", "contact_phone", "TEXT"),
    # отзыв отклика: поле status ограничено четырьмя значениями, отзыв хранится отдельной отметкой
    ("applications", "withdrawn_at", "TEXT"),
    ("applications", "status_changed_at", "TEXT"),
    # название вакансии на момент отклика: переписка остаётся понятной, когда вакансию удалили по сроку
    ("conversations", "title", "TEXT NOT NULL DEFAULT ''"),
    # копия сообщения и подпись жалобы (JSON): модерация видит текст, даже если сообщение потом удалят
    ("reports", "snapshot", "TEXT NOT NULL DEFAULT ''"),
    # письма о новых откликах и сообщениях можно отключить
    ("users", "notify_responses", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "notify_messages", "INTEGER NOT NULL DEFAULT 1"),
)

INDEXES = (
    ("idx_app_user", "applications", "user_id, created_at"),
    ("idx_conv_employer", "conversations", "employer_user_id, last_message_at"),
    ("idx_conv_seeker", "conversations", "seeker_user_id, last_message_at"),
    ("idx_msg_unread", "messages", "conversation_id, read_at, sender_user_id"),
    ("idx_reports_target", "reports", "target_type, target_id"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in COLUMNS:
        have = _columns(conn, table)
        if have and column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    for name, table, columns in INDEXES:
        if _columns(conn, table):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    conn.commit()
