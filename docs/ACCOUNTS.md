# Аккаунты

Два вида аккаунтов: соискатель (`job_seeker`) и работодатель (`employer`), плюс администратор (`admin`). Почту подтверждают все: без подтверждения нельзя войти, размещать вакансии и писать.

## Подключение

```python
from rabota.web.auth_routes import install
install(app, db_path=...)   # маршруты, middleware сессии и CSRF, шаблоны
```

После этого в каждом запросе есть `request.state.user` (словарь или `None`) и `request.state.csrf_token` (значение скрытого поля `csrf_token` для любой POST-формы). Зависимости для маршрутов: `rabota.accounts.deps` (`get_current_user`, `require_user`, `require_verified`, `require_role("employer")`). Гость уходит на `/login?next=...`, чужая роль получает 403.

## Маршруты

`/register`, `/login`, `POST /logout`, `/verify/<токен>`, `/verify/resend`, `/forgot`, `/reset/<токен>`, `/account` (профиль, пароль, смена почты, удаление), `/email/confirm/<токен>`, `/cabinet` (перенаправление по роли).

## Безопасность

- Пароли: `hashlib.scrypt` с солью, сравнение в постоянное время, минимум 8 знаков, список частых паролей.
- Токены писем: `secrets.token_urlsafe`, в базе только SHA-256, подтверждение и смена почты 24 часа, сброс пароля 1 час, одноразовые.
- Сессия: случайный токен в cookie (HttpOnly, SameSite=Lax, Secure по https), в базе хеш, 30 дней с продлением при активности.
- CSRF: подписанное зерно в cookie и в форме, плюс проверка Origin.
- Лимиты частоты по IP и по почте (в памяти процесса), одинаковые ответы для известной и неизвестной почты.

## Почта

`MAIL_BACKEND=file` (по умолчанию) складывает письма в `data/outbox/*.eml`, `MAIL_BACKEND=smtp` отправляет через `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` (`starttls`, `ssl`, `none`). Ссылки в письмах строятся от `SITE_URL`. Образец в `.env.example`.

## Администратор

`.venv/bin/python scripts/create_admin.py admin@example.ru` (пароль спрашивается без показа на экране).
