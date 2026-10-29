# Сайт

Серверный рендер: FastAPI, Jinja2, чистый CSS, минимум JavaScript. Код в `rabota/web/`.

## Запуск

```
.venv/bin/uvicorn rabota.web.app:app --port 8000
```

База берётся из `DB_PATH` (по умолчанию `data/rabota.db`), таблица `vacancies`. Для разработки и скриншотов есть выдуманные данные:

```
.venv/bin/python scripts/seed_demo.py              # data/demo.db
DB_PATH=data/demo.db .venv/bin/uvicorn rabota.web.app:app --port 8000
```

За прокси (nginx) для лимита частоты по адресу клиента задайте `WEB_TRUST_PROXY=1`.

## Страницы

| Адрес | Что |
| --- | --- |
| `/` | поиск, быстрые условия, сферы, свежие вакансии |
| `/vacancies` | лента с фильтрами и страницами |
| `/vacancy/<id>` | карточка, контакты, "также публикуют", похожие, жалоба |
| `/profession/<название>`, `/district/<название>`, `/city/<название>` | страницы для поисковиков |
| `/razmestit` | форма размещения, вакансия сохраняется со статусом `hidden` до проверки |
| `/about`, `/sitemap.xml`, `/robots.txt` | служебные |

В ленту попадают только `kind="vacancy"`, `status="active"`, без `duplicate_of` и без пометки `suspicious`.

## Оформление

Токены цвета, шрифта, отступов, радиусов и теней лежат в `rabota/web/static/tokens.css`, компоненты в `site.css` используют только их. Тёмная тема включается системной настройкой и переключателем. Шрифт Onest лежит в `static/fonts/`.

## Проверки

`.venv/bin/pytest` (маршруты, запросы, оформление) и `.venv/bin/pytest -m e2e` (Chromium: мобильная и десктопная ширина, темы, формы, доступность).
