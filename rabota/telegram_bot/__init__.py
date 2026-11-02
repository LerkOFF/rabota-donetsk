"""Telegram через Bot API по токену бота: ответы на команды, доставка подписчикам, автопостинг в канал.

Приложение Telegram (api_id, api_hash) не используется. Все запросы идут на api.telegram.org с токеном бота из .env,
через TELEGRAM_PROXY, если он задан. Описание: docs/ALERTS.md.
"""
