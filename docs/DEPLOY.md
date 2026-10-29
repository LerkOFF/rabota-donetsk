# Запуск на сервере

Пошаговая инструкция: от пустого VPS (или сервера с другими сайтами) до работающего сайта, проверок, обновлений и отката. Все файлы для сервера лежат в `deploy/` и `scripts/`, значения настроек в `.env` на сервере.

## Схема

```
посетитель --443--> Xray (TLS по SNI, сертификаты Xray) --fallback, PROXY v2--> nginx 127.0.0.1:8081 (h2) / 8082
                                                                                  |
                                                      /static, /media, редиректы  |  proxy_pass
                                                                                  v
                                                              uvicorn 127.0.0.1:8000 (rabota-web)
посетитель --80---> nginx: /.well-known/acme-challenge/ и 301 на https

воркер (rabota-worker): Telegram, VK, открытые базы -> та же база SQLite (WAL) -> сайт
таймеры: резервная копия базы (ночью), проверка источников и уведомления (каждые 5 минут)
```

Если на сервере нет Xray и TLS терминирует сам nginx, в `deploy/nginx-rabota.conf` нужно заменить блоки `listen 127.0.0.1:8081/8082 proxy_protocol` на `listen 443 ssl http2` с путями к сертификату и убрать `set_real_ip_from` и `real_ip_header proxy_protocol`. Остальное не меняется.

## Что понадобится

| Что | Зачем |
| --- | --- |
| Сервер Ubuntu 24.04 или Debian 12+, 1 vCPU, 1 ГБ свободной памяти, 5 ГБ диска | сайт, воркер, база, копии |
| Системный Python 3.12 или новее | запуск приложения |
| nginx, certbot, git | ставятся заранее, установочный скрипт их не ставит и чужие сайты не трогает |
| Домен и DNS | `работа-донецк.рф` и `www` указывают на IP сервера (A, при IPv6 ещё AAAA) |
| Почтовый сервис с SMTP | письма подтверждения адреса и сброса пароля |
| Данные оператора | `OPERATOR_*` для юридических текстов, см. `docs/legal/CHECKLIST.md` |
| Ключи источников | VK, Telegram (`TG_API_ID`, `TG_API_HASH`), ключ языковой модели для афиш |
| Прокси для Telegram | если с сервера `api.telegram.org` недоступен: адрес и учётные данные задаются в `.env` на сервере (`TELEGRAM_PROXY`) |

Адреса в Интернете и учётные данные прокси в репозиторий не попадают.

## Запуск с нуля

1. DNS. Проверить, что `работа-донецк.рф` и `www.работа-донецк.рф` указывают на сервер: `dig +short xn----7sbadrg9awfdp1a2d.xn--p1ai`.
2. Установка. На сервере от root:

   ```bash
   git clone https://github.com/LerkOFF/rabota-donetsk.git /tmp/rabota-bootstrap
   sudo bash /tmp/rabota-bootstrap/scripts/bootstrap-server.sh
   ```

   Скрипт доустанавливает недостающие мелкие пакеты, создаёт пользователя `rabota`, папку `/var/www/rabota` (код), `data/` с правами 700, `media/`, `/var/backups/rabota`, клонирует репозиторий, ставит зависимости по `uv.lock`, создаёт `.env` с новыми `SECRET_KEY` и `HEALTH_TOKEN`, ставит службы и таймеры, добавляет свой виртуальный хост nginx `site-rabota`. Повторный запуск безопасен. Параметры: `--repo`, `--branch`, `--cert`, `--email`, `--no-start`.
3. Настройки. Открыть `/var/www/rabota/.env` и заполнить по таблице ниже. Проверка (секреты не печатаются):

   ```bash
   sudo -u rabota /var/www/rabota/.venv/bin/python /var/www/rabota/scripts/preflight.py
   ```

   Ошибки надо исправить, предупреждения прочитать. Перед открытием регистрации запустить с `--strict`: тогда незаполненные данные оператора тоже считаются ошибкой.
4. Сертификат (до этого шага сайт по https не открывается):

   ```bash
   sudo certbot certonly --webroot -w /var/www/acme -d xn----7sbadrg9awfdp1a2d.xn--p1ai -d www.xn----7sbadrg9awfdp1a2d.xn--p1ai
   sudo /var/www/rabota/scripts/xray-cert-add.sh xn----7sbadrg9awfdp1a2d.xn--p1ai --dry-run   # посмотреть, что изменится
   sudo /var/www/rabota/scripts/xray-cert-add.sh xn----7sbadrg9awfdp1a2d.xn--p1ai
   ```

   `xray-cert-add.sh` копирует сертификат в `/usr/local/etc/xray/certs/`, сохраняет `config.json.bak-<время>` рядом с конфигурацией, добавляет пару файлов в `certificates` нужного inbound (тег по умолчанию `vless-tcp`, меняется переменной `XRAY_INBOUND_TAG`), проверяет JSON и `xray -test`, добавляет блок в deploy-хук certbot `/etc/letsencrypt/renewal-hooks/deploy/xray-certs.sh` (перед перезапуском Xray) и перезапускает `xray`. Если проверка или перезапуск не удались, возвращается прежний `config.json`. Повторный запуск ничего не дублирует. Продление проверить: `sudo certbot renew --dry-run`.
5. Перезапуск и первый администратор:

   ```bash
   sudo systemctl restart rabota-web rabota-worker
   sudo -u rabota /var/www/rabota/.venv/bin/python /var/www/rabota/scripts/create_admin.py admin@домен
   ```

   Пароль спрашивается без показа на экране.
6. Вход в Telegram для воркера (один раз, интерактивно, с `TELEGRAM_PROXY` в `.env`, если он нужен):

   ```bash
   sudo -u rabota /var/www/rabota/.venv/bin/python -m rabota tg-login
   sudo systemctl restart rabota-worker
   ```

   Подробности и подписка аккаунта на каналы: `docs/REALTIME.md`.
7. Проверки: раздел "Проверки после запуска".

## Переменные окружения

Образец со всеми переменными: `.env.example`. Файл `.env` читают и приложение, и службы systemd. Права 600, владелец `rabota`.

| Переменная | Назначение | Обязательна на проде |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | да |
| `DEBUG` | отладка; на проде `0` | да |
| `SITE_URL` | адрес сайта с `https://`, из него canonical, sitemap, письма, HSTS | да |
| `WEB_TRUST_PROXY` | `1`: адрес клиента берётся из `X-Forwarded-For`, который nginx перезаписывает; включать только когда приложение слушает `127.0.0.1` за nginx | да |
| `SECRET_KEY` | ключ подписи форм, 32+ символа; пусто: создаётся `data/.secret_key` | рекомендуется |
| `DB_PATH` | путь к SQLite; по умолчанию `data/rabota.db` | нет |
| `MAIL_BACKEND`, `SMTP_*` | `smtp` и параметры почтового сервиса | да (без них письма не уйдут) |
| `OPERATOR_NAME`, `OPERATOR_STATUS`, `OPERATOR_INN`, `OPERATOR_EMAIL`, `OPERATOR_ADDRESS` | данные оператора в юридических текстах | до открытия регистрации |
| `CONTACT_EMAIL`, `SITE_NAME` | почта в подвале, название | нет |
| `VK_ACCESS_TOKEN` | чтение стен VK | для VK |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_PATH`, `TG_CHANNELS_FILE`, `TG_SWEEP_SECONDS` | слушатель Telegram | для Telegram |
| `TELEGRAM_PROXY` | `socks5://host:port` или `socks5://user:pass@host:port`; используется слушателем, сборщиком страниц t.me и уведомлениями. Старое имя `TG_PROXY` тоже читается | если Telegram с сервера закрыт |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL_TEXT`, `LLM_MODEL_VISION` | разбор афиш-картинок | для картинок |
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET`, `HH_USER_AGENT` | hh.ru | нет |
| `MEDIA_DIR`, `HEARTBEAT_PATH`, `WORKER_EXPIRE` | служебные пути воркера | нет |
| `VACANCY_TTL_DAYS`, `EXPIRED_KEEP_DAYS` | срок жизни вакансии и хранение скрытых | нет |
| `LOG_FORMAT` | `json` для journald | рекомендуется (службы задают сами) |
| `HEALTH_TOKEN` | токен для подробностей `/healthz/deep` | рекомендуется |
| `ALERT_TG_BOT_TOKEN`, `ALERT_TG_CHAT_ID` | уведомления о сбоях в Telegram-бот; без них выключено | нет |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS`, `BACKUP_GPG_RECIPIENT`, `BACKUP_RSYNC_TARGET` | резервные копии | нет |
| `METRIKA_ID`, `METRIKA_WEBVISOR` | Яндекс Метрика | нет |
| `WEBMASTER_VERIFY_FILE`, `WEBMASTER_VERIFY_BODY` | файл подтверждения Вебмастера | нет |

Безопасность cookie задана в коде: сессия и CSRF-ключ `HttpOnly`, `SameSite=Lax`, `Secure` при запросе по https (nginx передаёт `X-Forwarded-Proto: https`, uvicorn запущен с `--proxy-headers` только для `127.0.0.1`).

## Проверки после запуска

```bash
sudo systemctl status rabota-web rabota-worker rabota-backup.timer rabota-alert.timer
curl -s http://127.0.0.1:8000/healthz                      # ok
/var/www/rabota/scripts/smoke.sh https://xn----7sbadrg9awfdp1a2d.xn--p1ai
```

`smoke.sh` проверяет коды ответа основных страниц, `title`, `canonical`, `robots.txt`, `sitemap.xml`, `/healthz` и `/healthz/deep`, заголовки безопасности и HSTS, сжатие, кеш статики, закрытые пути (`/admin`, `/.env`, `/.git/config`), редиректы с http и с www. До смены DNS: `SMOKE_RESOLVE=хост:443:IP`. Сразу после установки воркер ещё не отработал, поэтому `SMOKE_DEEP=warn`.

Руками стоит открыть сайт в браузере на телефоне и компьютере в светлой и тёмной темах, зарегистрировать тестовый аккаунт и убедиться, что письмо пришло.

## Наблюдаемость

- `GET /healthz` отдаёт `ok`, если процесс жив.
- `GET /healthz/deep` проверяет базу, сердцебиение воркера и свежесть источников. Без токена возвращает только `status` (`ok`, `degraded`, `fail`) и состояние частей. С заголовком `X-Health-Token: <HEALTH_TOKEN>` добавляет названия источников, возраст, ошибки (без секретов) и счётчики. Код 503 только при `fail`: база недоступна, воркер молчит или целая группа источников просрочена. Результат кешируется на 15 секунд.
- Внешний мониторинг: UptimeRobot или Uptime Kuma, монитор HTTP(s) на `https://xn----7sbadrg9awfdp1a2d.xn--p1ai/healthz/deep` (ловит 503) или с ключевым словом `"status":"ok"`. Скрипт `scripts/check-health.sh` для Zabbix и cron возвращает коды 0, 1, 2 как плагины Nagios.
- Уведомления в Telegram: `rabota-alert.timer` раз в 5 минут вызывает `python -m rabota.ops.alerts`. Если в `.env` есть `ALERT_TG_BOT_TOKEN` и `ALERT_TG_CHAT_ID`, о новой проблеме, восстановлении и (раз в 6 часов) о неисчезнувшей проблеме приходит сообщение. Без токена ничего не происходит. Запрос к Telegram идёт через `TELEGRAM_PROXY`. Проверка текста без отправки: `python -m rabota.ops.alerts --dry-run`.
- Журналы: службы пишут в journald в формате JSON (`LOG_FORMAT=json`), например `journalctl -u rabota-web -o cat | jq .`. Ошибки приложения: `journalctl -u rabota-web -p err --since today`. Журналы nginx этого сайта: `/var/log/nginx/rabota.access.log` и `rabota.error.log` (ротирует штатный logrotate nginx). Журнал развёртываний `/var/log/rabota/deploy.log` ротируется `deploy/logrotate-rabota`.
- Блокировка злоупотреблений (необязательно): `deploy/fail2ban/` содержит фильтр по отказам `limit_req` и выключенный по умолчанию jail.

## Резервные копии

| Что | Как |
| --- | --- |
| Расписание | `rabota-backup.timer`, каждую ночь около 03:30 (с разбросом до 10 минут) |
| Способ | `scripts/backup.sh`: согласованный снимок работающей базы (`sqlite3 .backup`, при отсутствии `sqlite3` тот же механизм через Python), проверка `PRAGMA integrity_check`, сжатие, файл `/var/backups/rabota/rabota-<время>.db.gz` |
| Дополнительно | `rabota-<время>-files.tar.gz` с сессией Telegram, списком каналов и ключом форм, если они есть |
| Хранение | 14 дней (`BACKUP_KEEP_DAYS`); для юридических текстов срок ротации не больше 30 дней |
| Проверка | после каждой копии `scripts/verify-backup.sh` разворачивает её во временную папку, проверяет целостность и наличие таблиц; сбой делает службу `rabota-backup` неуспешной |
| Шифрование и вынос | `BACKUP_GPG_RECIPIENT` (открытый ключ gpg), `BACKUP_RSYNC_TARGET` (хост:папка на другом сервере в России) |

Восстановление:

```bash
sudo /var/www/rabota/scripts/verify-backup.sh                                        # свежая копия в порядке?
sudo /var/www/rabota/scripts/restore.sh /var/backups/rabota/rabota-<время>.db.gz --target /tmp/check.db   # проверить без замены
sudo /var/www/rabota/scripts/restore.sh /var/backups/rabota/rabota-<время>.db.gz --yes                    # заменить боевую базу
```

`restore.sh --yes` останавливает `rabota-web` и `rabota-worker`, сохраняет текущую базу как `rabota.db.before-restore-<время>`, ставит копию на место и запускает службы. Раз в квартал стоит проверять восстановление на чистой копии вручную.

## Обновление

```bash
sudo /var/www/rabota/scripts/deploy.sh                                 # обновить и перезапустить
sudo /var/www/rabota/scripts/deploy.sh --smoke https://xn----7sbadrg9awfdp1a2d.xn--p1ai
```

Скрипт идемпотентен. Порядок: блокировка от параллельного запуска, `git fetch` и fast-forward, `uv sync --frozen --no-dev` (по `uv.lock`), резервная копия базы, `preflight.py --smoke --migrate` (настройки, дымовая проверка приложения на временной базе, доведение схемы боевой базы), обновление юнитов systemd и виртуального хоста `site-rabota` (с проверкой `nginx -t` и возвратом прежней версии при ошибке), перезапуск служб, ожидание `/healthz` до 40 секунд. Если зависимости, проверка или запуск не удались, код возвращается на прежний коммит, службы перезапускаются на нём, в журнал выводится путь последней копии базы. Схема базы только дополняется, поэтому прежний код работает с новой схемой; базу при откате вручную возвращают `restore.sh`, если нужно.

Параметры: `--force` (перезапустить без изменений), `--no-pull`, `--skip-nginx`, `--smoke URL`. Переменная `DEPLOY_RUN_TESTS=1` дополнительно прогоняет тесты безопасности, если в окружении есть pytest.

Ручной откат на конкретную версию: `sudo -u rabota git -C /var/www/rabota reset --hard <коммит>`, затем `sudo /var/www/rabota/scripts/deploy.sh --no-pull --force`.

## Хватит ли SQLite

На первом этапе да. Причины: приложение читает много и пишет мало (воркер добавляет единицы вакансий в минуту, формы и кабинеты дают десятки записей в час), вакансии живут 21 день и скрытые удаляются через 30, поэтому база остаётся в пределах сотен мегабайт, режим WAL позволяет сайту читать, пока воркер пишет, ожидание блокировки включено, сайт работает в одном процессе. Резервное копирование делается целиком за секунды.

Порог перехода на PostgreSQL (схема спроектирована переносимой, переписывать заранее не нужно). Переходить, когда выполняется любое из условий:

| Признак | Порог |
| --- | --- |
| Ошибки `database is locked` в журнале | чаще нескольких в сутки при обычной нагрузке |
| Постоянная нагрузка на запись (отклики, сообщения, воркер) | больше 20 записей в секунду длительно |
| Размер базы | больше 2 ГБ или копия дольше минуты |
| Число процессов сайта | нужно больше одного процесса или больше одного сервера |
| Полнотекстовый поиск и подсказки | нужны `pg_trgm` и ранжирование на десятках тысяч активных вакансий |
| Допустимая потеря данных | нужна меньше суток (потоковая репликация и архив WAL) |
| Отказоустойчивость | нужен горячий резерв |

План перехода: PostgreSQL 16 на том же сервере или управляемый в России, перенос данных `pgloader`, переключение через `DATABASE_URL` в слое доступа к данным (потребует адаптера `rabota/db.py` и запросов `rabota/web/queries.py`), прогон тестов на PostgreSQL, окно простоя на перенос не больше 15 минут.

## Яндекс Метрика и Вебмастер

1. Создать счётчик в Метрике, номер записать в `METRIKA_ID`, перезапустить `rabota-web`.
2. Появится окно согласия на cookie (светлая и тёмная темы, телефон и компьютер). До нажатия "Принять аналитику" код счётчика не загружается и сторонних запросов нет; кнопка "Только необходимые" равноценна по заметности; выбор можно изменить ссылкой "Настройки cookie" в подвале; при отказе после согласия cookie Метрики удаляются. Запись сессий (вебвизор) на страницах входа, регистрации и кабинета отключена всегда; общее включение `METRIKA_WEBVISOR=1` или `0`.
3. Политика CSP расширяется адресами Метрики автоматически только при заданном `METRIKA_ID`. После запуска проверить в консоли браузера отсутствие ошибок CSP и при необходимости дополнить список в `rabota/web/security.py`.
4. В тексте `docs/legal/cookies.md` указать название сервиса аналитики (Яндекс Метрика) и обновить дату редакции.
5. Вебмастер: подтвердить сайт через Метрику; запасной вариант - HTML-файл. Скачать файл вида `yandex_0123abcd.html`, записать его имя в `WEBMASTER_VERIFY_FILE`, перезапустить сайт, нажать "Проверить". Если нужен другой текст файла, он задаётся в `WEBMASTER_VERIFY_BODY`.

Без `METRIKA_ID` в страницы ничего не вставляется, окна согласия нет.

## Лимиты и защита

- nginx: зоны `rabota_auth` (вход, регистрация, сброс пароля: 10 POST в минуту с адреса), `rabota_forms` (остальные POST: 30 в минуту), `rabota_pages` (20 запросов в секунду), `rabota_health` (мониторинг). Отказ: код 429. В приложении есть собственные лимиты по почте и адресу.
- Заголовки безопасности: CSP, Referrer-Policy, Permissions-Policy, nosniff, запрет встраивания, COOP добавляет приложение; HSTS (`max-age=31536000`, без `includeSubDomains` и `preload`) и nosniff выставляет nginx на всех ответах и убирает копии приложения. Расширять HSTS на поддомены и подавать в preload-список стоит после недели работы без ошибок.
- Админка закрыта входом; чтобы пустить только свои адреса, в `deploy/nginx-rabota.conf` раскомментировать `allow` и `deny all` в `location ^~ /admin`.
- Копии фото из Telegram (`data/media`) наружу не отдаются. Папка `/media/` отдаёт только загруженные файлы из `/var/www/rabota/media`.
- Службы запущены от пользователя `rabota` с песочницей systemd: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, запись только в `data/` и `media/`, лимиты памяти (сайт 350 МБ, воркер 450 МБ). Каталог `data/` доступен только владельцу.

## Состав файлов

| Файл | Назначение |
| --- | --- |
| `deploy/nginx-rabota.conf` | виртуальный хост (порты 8081, 8082 за Xray, порт 80 для ACME), лимиты, статика, страницы ошибок |
| `deploy/errors/50x.html` | страница при недоступном приложении |
| `deploy/rabota-web.service`, `rabota-worker.service` | сайт и воркер |
| `deploy/rabota-backup.service` и `.timer` | ночная копия с проверкой |
| `deploy/rabota-alert.service` и `.timer` | проверка источников и уведомления |
| `deploy/rabota-expire.service` и `.timer` | только если воркер запущен с `WORKER_EXPIRE=0` |
| `deploy/rabota-tg-proxy.service` | туннель SOCKS5 через SSH, если нет готового прокси |
| `deploy/uvicorn-log.json` | JSON-журнал сайта |
| `deploy/logrotate-rabota`, `deploy/fail2ban/` | ротация журналов, необязательная блокировка |
| `scripts/bootstrap-server.sh`, `deploy.sh` | первичная установка и обновление |
| `scripts/backup.sh`, `verify-backup.sh`, `restore.sh` | копии и восстановление |
| `scripts/smoke.sh`, `check-health.sh` | проверка сайта и внешний мониторинг |
| `scripts/xray-cert-add.sh` | сертификат в Xray |
| `scripts/preflight.py` | проверка настроек, дымовая проверка, миграция схемы |

## Если что-то не так

| Симптом | Что смотреть |
| --- | --- |
| Сайт не открывается по https | сертификат добавлен в Xray (`xray-cert-add.sh`), `systemctl status xray`, `openssl s_client -connect домен:443 -servername домен` |
| 502 или страница "временно недоступен" | `systemctl status rabota-web`, `journalctl -u rabota-web -n 100 -o cat` |
| Все посетители с одного адреса, лимиты срабатывают у всех | `WEB_TRUST_PROXY=1`, в vhost `proxy_set_header X-Forwarded-For $remote_addr`, `set_real_ip_from` и `proxy_protocol` совпадают с Xray |
| Не приходят письма | `MAIL_BACKEND=smtp`, `SMTP_*`, `journalctl -u rabota-web`; проверка `preflight.py` |
| `/healthz/deep` отдаёт 503 | воркер: `systemctl status rabota-worker`; подробности с токеном: `curl -H "X-Health-Token: ..." .../healthz/deep` |
| Telegram молчит | `TELEGRAM_PROXY`, доступность прокси, `docs/REALTIME.md` |
| `deploy.sh` откатился | `/var/log/rabota/deploy.log` и `journalctl -u rabota-web`, причина в последних строках |
