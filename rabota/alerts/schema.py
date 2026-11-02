"""Таблицы подписок на вакансии, отправленного и журнала доставки. Вызывается из rabota.db.connect, повтор безопасен."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  email TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
  channel TEXT NOT NULL DEFAULT 'email' CHECK (channel IN ('email','telegram')),
  frequency TEXT NOT NULL DEFAULT 'daily' CHECK (frequency IN ('instant','daily')),
  filters TEXT NOT NULL DEFAULT '{}',
  filters_key TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','paused','unsubscribed')),
  baseline_id INTEGER NOT NULL DEFAULT 0,
  code_hash TEXT NOT NULL DEFAULT '', code_expires_at TEXT, code_attempts INTEGER NOT NULL DEFAULT 0,
  tg_chat_id INTEGER, tg_code_hash TEXT NOT NULL DEFAULT '', tg_code_expires_at TEXT,
  created_at TEXT NOT NULL, confirmed_at TEXT, last_sent_at TEXT, last_checked_at TEXT,
  sent_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
  unsubscribed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_sub_user ON alert_subscriptions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_alert_sub_email ON alert_subscriptions(email, status);
CREATE INDEX IF NOT EXISTS idx_alert_sub_chat ON alert_subscriptions(tg_chat_id, status);
CREATE INDEX IF NOT EXISTS idx_alert_sub_due ON alert_subscriptions(status, channel, frequency);
CREATE INDEX IF NOT EXISTS idx_alert_sub_tgcode ON alert_subscriptions(tg_code_hash);
-- что уже отправлено по подписке: повторно вакансия не уходит
CREATE TABLE IF NOT EXISTS alert_sent (
  subscription_id INTEGER NOT NULL REFERENCES alert_subscriptions(id) ON DELETE CASCADE,
  vacancy_id INTEGER NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY (subscription_id, vacancy_id)
);
-- журнал доставки для лимита писем в час и для админки: digest, confirm, telegram
CREATE TABLE IF NOT EXISTS alert_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, subscription_id INTEGER,
  channel TEXT NOT NULL, kind TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1,
  error TEXT NOT NULL DEFAULT '', vacancies INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_log_at ON alert_log(channel, kind, at);
-- автопостинг в канал: каждая попытка и что именно показано (по рубрике вакансия попадает в канал один раз)
CREATE TABLE IF NOT EXISTS channel_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, rubric TEXT NOT NULL, post_key TEXT NOT NULL,
  vacancy_ids TEXT NOT NULL DEFAULT '[]', ok INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '',
  message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_channel_posts_at ON channel_posts(at);
CREATE INDEX IF NOT EXISTS idx_channel_posts_key ON channel_posts(post_key, ok);
CREATE TABLE IF NOT EXISTS channel_vacancies (
  rubric TEXT NOT NULL, vacancy_id INTEGER NOT NULL, posted_at TEXT NOT NULL,
  PRIMARY KEY (rubric, vacancy_id)
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
