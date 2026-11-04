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
| `tesseract-ocr` и `tesseract-ocr-rus` | чтение афиш-картинок без платной модели (`LLM_PROVIDER=ocr`); `bootstrap-server.sh` их ставит, проверка: `tesseract --list-langs` показывает `rus` и `eng` |
| nginx, certbot, git | ставятся заранее, установочный скрипт их не ставит и чужие сайты не трогает |
| Домен и DNS | `работа-донецк.рф` и `www` указывают на IP сервера (A, при IPv6 ещё AAAA) |
| Почтовый сервис с SMTP | письма с кодом подтверждения при регистрации и кодом сброса пароля, см. раздел "Почта" |
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
| `MAIL_BACKEND`, `SMTP_*` | `smtp` и параметры почтового сервиса, подробности в разделе "Почта" | да (без них письма не уйдут) |
| `OPERATOR_NAME`, `OPERATOR_STATUS`, `OPERATOR_INN`, `OPERATOR_EMAIL`, `OPERATOR_ADDRESS` | данные оператора в юридических текстах | до открытия регистрации |
| `CONTACT_EMAIL`, `SITE_NAME` | почта в подвале, название | нет |
| `VK_ACCESS_TOKEN` | чтение стен VK | для VK |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_PATH`, `TG_CHANNELS_FILE`, `TG_SWEEP_SECONDS` | слушатель Telegram | для Telegram |
| `TELEGRAM_PROXY` | `socks5://host:port` или `socks5://user:pass@host:port`; используется слушателем, сборщиком страниц t.me и уведомлениями. Старое имя `TG_PROXY` тоже читается | если Telegram с сервера закрыт |
| `TG_BOT_TOKEN`, `TG_BOT_USERNAME`, `TG_CHANNEL_ID`, `TG_CHANNEL_USERNAME` | бот и канал для подписок: только Bot API, ответы бота и автопостинг идут из воркера через `TELEGRAM_PROXY`. Пустой токен: всё выключено | для Telegram-подписок |
| `ALERTS_MAIL_PER_HOUR`, `ALERTS_MAIL_PAUSE`, `ALERTS_MAX_PER_USER`, `CHANNEL_POSTS_PER_HOUR` | стартовые значения лимитов подписок и канала, дальше правятся в админке | нет |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL_TEXT`, `LLM_MODEL_VISION` | разбор афиш-картинок; `LLM_PROVIDER=ocr` читает афиши Tesseract без ключа (на 1 vCPU один постер читается 30-70 секунд) | для картинок |
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET`, `HH_USER_AGENT` | hh.ru | нет |
| `MEDIA_DIR`, `HEARTBEAT_PATH`, `WORKER_EXPIRE` | служебные пути воркера | нет |
| `VACANCY_TTL_DAYS`, `EXPIRED_KEEP_DAYS` | срок жизни вакансии и хранение скрытых | нет |
| `LOG_FORMAT` | `json` для journald | рекомендуется (службы задают сами) |
| `LOG_FILE` | файл журнала, который админка показывает в разделе «Система» (только чтение; пусто: источник не показывается) | нет |
| `HEALTH_TOKEN` | токен для подробностей `/healthz/deep` | рекомендуется |
| `ALERT_TG_BOT_TOKEN`, `ALERT_TG_CHAT_ID` | уведомления о сбоях в Telegram-бот; без них выключено | нет |
| `MODERATION_TG_CHAT_ID`, `MODERATION_TG_BOT_TOKEN` | личное сообщение в Telegram, когда вакансия работодателя пришла на модерацию (новая или после правки); без `MODERATION_TG_CHAT_ID` выключено, токен по умолчанию `ALERT_TG_BOT_TOKEN`, затем `TG_BOT_TOKEN`; получатель должен нажать Start у бота | нет |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS`, `BACKUP_GPG_RECIPIENT`, `BACKUP_RSYNC_TARGET` | резервные копии | нет |
| `METRIKA_ID`, `METRIKA_WEBVISOR` | Яндекс Метрика | нет |
| `WEBMASTER_VERIFY_FILE` | имя файла подтверждения Яндекс Вебмастера, например `yandex_0123abcd.html` | нет |
| `GOOGLE_VERIFY_FILE` | имя файла подтверждения Google Search Console, например `google0123abcd0123abcd.html` (содержимое сайт отдаёт сам) | нет |
| `INDEXNOW_KEY`, `INDEXNOW_STATE_PATH` | ключ IndexNow и файл состояния отправки | нет |
| `SERVICE_CITIES` | города, вакансии которых видны на сайте, через запятую | Донецк |
| `WEBMASTER_VERIFY_FILE`, `WEBMASTER_VERIFY_BODY` | файл подтверждения Вебмастера | нет |

Безопасность cookie задана в коде: сессия и CSRF-ключ `HttpOnly`, `SameSite=Lax`, `Secure` при запросе по https (nginx передаёт `X-Forwarded-Proto: https`, uvicorn запущен с `--proxy-headers` только для `127.0.0.1`).

## Почта

Сайт отправляет служебные письма: шестизначный код при регистрации и при сбросе пароля, уведомления об изменении пароля и почты, о новых откликах и сообщениях. Служебные письма идут только по запросу человека, поэтому в них нет `List-Unsubscribe` и `Reply-To`. Исключение: рассылка новых вакансий по подписке (см. `docs/ALERTS.md`), у неё есть `List-Unsubscribe` с одним щелчком и `Precedence: bulk`. Каждое письмо двухчастное (текст и HTML), в UTF-8, с `Date`, `Message-ID` на домене отправителя, заголовками `Auto-Submitted: auto-generated` и `X-Auto-Response-Suppress: All`. Имя отправителя "Работа Донецк". Домены с кириллицей (`SMTP_FROM`, адрес получателя) отправляются в punycode.

### Переменные

| Переменная | Значение |
| --- | --- |
| `MAIL_BACKEND` | `smtp` на проде. `file`: письма складываются в `data/outbox/*.eml`, только для разработки |
| `SMTP_HOST`, `SMTP_PORT` | сервер и порт. Локальный Exim на том же сервере: `127.0.0.1` и `25` |
| `SMTP_SECURITY` | `starttls` (587), `ssl` (465) или `none`. Для `127.0.0.1:25` без шифрования: `none` |
| `SMTP_USER`, `SMTP_PASSWORD` | вход на SMTP. Для локального релея без входа пусты |
| `SMTP_FROM` | адрес отправителя, домен сайта в punycode, например `noreply@xn----7sbadrg9awfdp1a2d.xn--p1ai` |
| `CONTACT_EMAIL` | адрес для подвала сайта: настоящий ящик, который читают |
| `OPERATOR_EMAIL` | почта оператора в юридических текстах |

### Что нужно в DNS домена отправителя

Для домена, указанного в `SMTP_FROM` (в примере ниже `DOMAIN` это punycode-имя сайта, `IP` это адрес сервера, с которого уходят письма):

| Тип | Имя | Значение |
| --- | --- | --- |
| TXT | `@` | `v=spf1 ip4:IP -all` (если у домена уже есть SPF, добавить `ip4:IP` в него) |
| TXT | `mail._domainkey` | `v=DKIM1; k=rsa; p=<открытый ключ>` |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:<ящик на домене>` |
| MX | `@` | почтовый сервер, если на домене должны приниматься входящие (обязателен для `CONTACT_EMAIL` на этом домене) |

Открытый ключ DKIM берётся из закрытого, который лежит на почтовом сервере (Exim подписывает по домену из `From`, селектор `mail`). Закрытый ключ не покидает сервер:

```bash
openssl rsa -in /etc/exim4/dkim/DOMAIN.private -pubout -outform PEM 2>/dev/null | grep -v -- '-----' | tr -d '\n'; echo
```

Подпись надо включать только после того, как TXT появился на авторитативных серверах имён: иначе у получателей будет `dkim=fail`. Проверка: `dig +short TXT mail._domainkey.DOMAIN @ns1.reg.ru`. Обратная запись (PTR) IP сервера должна совпадать с именем в приветствии Exim (`EHLO`), прямая запись этого имени указывать на тот же IP.

### Проверка

```bash
sudo -u rabota /var/www/rabota/.venv/bin/python -m rabota mail-check
```

Команда ничего не отправляет: собирает письмо с кодом (пробным), печатает его заголовки и размеры частей и проверяет соединение с SMTP (подключение, EHLO, STARTTLS, вход). Одно проверочное письмо на нужный адрес: `python -m rabota mail-check --send-to адрес`. В заголовках полученного письма (Показать оригинал) должны быть `spf=pass`, `dkim=pass`, `dmarc=pass`. Сбои отправки пишутся в журнал (`journalctl -u rabota-web`) без текста письма и кода; на сайте человек при сбое видит просьбу повторить.

### Коды и лимиты

- Код шестизначный, в базе только HMAC-SHA256 с ключом `SECRET_KEY` (при смене ключа выданные коды перестают подходить, их запрашивают заново).
- Срок: 15 минут для подтверждения почты, 10 минут для сброса пароля. На один код 5 попыток ввода, на пятой неудаче код гаснет.
- Повторная отправка: не чаще раза в минуту; не больше 5 кодов в час на почту и назначение; не больше 20 запросов кода в час с одного адреса. Неверные вводы: не больше 5 за 15 минут на почту и 30 в час с адреса.
- Ответы сайта одинаковы для существующей и неизвестной почты: решение принимают счётчики в памяти процесса, а не наличие аккаунта.
- Запасной путь: ссылка подтверждения `/verify/<токен>` в письме регистрации (24 часа) и прежние ссылки `/reset/<токен>` продолжают работать.

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

На первом этапе да. Причины: приложение читает много и пишет мало (воркер добавляет единицы вакансий в минуту, формы и разделы работодателя дают десятки записей в час), вакансии живут 21 день и скрытые удаляются через 30, поэтому база остаётся в пределах сотен мегабайт, режим WAL позволяет сайту читать, пока воркер пишет, ожидание блокировки включено, сайт работает в одном процессе. Резервное копирование делается целиком за секунды.

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
2. Появится окно согласия на cookie (светлая и тёмная темы, телефон и компьютер). До нажатия "Принять аналитику" код счётчика не загружается и сторонних запросов нет; кнопка "Только необходимые" равноценна по заметности; выбор можно изменить ссылкой "Настройки cookie" в подвале; при отказе после согласия cookie Метрики удаляются. Запись сессий (вебвизор) на страницах входа, регистрации и профиля отключена всегда; общее включение `METRIKA_WEBVISOR=1` или `0`.
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
| Не приходят письма | `MAIL_BACKEND=smtp`, `SMTP_*`, `journalctl -u rabota-web`; `python -m rabota mail-check`; `preflight.py`; у получателя письмо может лежать в папке "Спам" (проверить SPF, DKIM, DMARC, раздел "Почта") |
| `/healthz/deep` отдаёт 503 | воркер: `systemctl status rabota-worker`; подробности с токеном: `curl -H "X-Health-Token: ..." .../healthz/deep` |
| Telegram молчит | `TELEGRAM_PROXY`, доступность прокси, `docs/REALTIME.md` |
| Афиши не читаются, посты помечены «нужна афиша» | `tesseract --list-langs` должен показывать `rus`; после установки или обновления распознавания `python -m rabota reparse-posters --dry-run`, затем без `--dry-run` |
| На сайте мусорная вакансия вместо афиши | `python -m rabota quality-sweep --dry-run`, затем без `--dry-run`: мусор уходит на проверку (раздел «Модерация») с причиной |
| `deploy.sh` откатился | `/var/log/rabota/deploy.log` и `journalctl -u rabota-web`, причина в последних строках |

## IndexNow: Яндекс узнаёт о новых вакансиях сразу

1. Сгенерировать ключ: `openssl rand -hex 16`, записать в `INDEXNOW_KEY` в `.env`, перезапустить `rabota-web`. Сайт начнёт отдавать ключ по `/<ключ>.txt`.
2. Таймер `rabota-indexnow` раз в 10 минут отправляет на `https://yandex.com/indexnow` адреса новых видимых вакансий и главных страниц ленты (состояние в `data/indexnow-state.json`). Без ключа ничего не отправляется. Проверка вручную: `python -m rabota.ops.indexnow --dry-run`.
3. Каждая новая страница обязана иметь SEO: тест `test_every_get_route_is_covered_and_has_full_seo` падает, если добавлен маршрут без `title`, `description`, `canonical`, Open Graph, структурированных данных или политики индексации.
