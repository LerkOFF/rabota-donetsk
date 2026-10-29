#!/usr/bin/env bash
# Первичная установка на сервер Ubuntu/Debian, где уже работают другие сайты. Мягкий к настроенному хосту:
# nginx, certbot и чужие vhost не ставит и не трогает, добавляет только свои файлы. Повторный запуск безопасен.
#
#   sudo bash scripts/bootstrap-server.sh [--repo URL] [--branch main] [--cert] [--email ПОЧТА] [--no-start]
#
# Что делает:
#   1. доставляет недостающие пакеты (git, curl, python3, python3-venv, rsync, sqlite3, openssl), uv ставит в /opt/uv-tool;
#   2. создаёт системного пользователя rabota и папки: /var/www/rabota (код), data (700), media, /var/backups/rabota, /var/log/rabota;
#   3. клонирует репозиторий, ставит зависимости по uv.lock;
#   4. создаёт .env из образца (только если его нет): ENVIRONMENT=production, новые SECRET_KEY и HEALTH_TOKEN, права 600;
#   5. ставит юниты systemd (web, worker, резервная копия, уведомления), logrotate;
#   6. кладёт vhost nginx site-rabota (только его), проверяет nginx -t, перезагружает nginx;
#   7. с --cert: выпускает сертификат certbot (webroot /var/www/acme) и добавляет его в Xray (scripts/xray-cert-add.sh).
# Дальше владелец заполняет .env (почта, ключи, прокси Telegram, данные оператора), см. docs/DEPLOY.md.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/var/www/rabota}"
# shellcheck source=scripts/lib-deploy.sh
source "$SELF_DIR/lib-deploy.sh"

REPO="${REPO_URL:-https://github.com/LerkOFF/rabota-donetsk.git}"
BRANCH="${DEPLOY_BRANCH:-main}"
want_cert=0 email="" start=1
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:?}"; shift ;;
    --branch) BRANCH="${2:?}"; shift ;;
    --cert) want_cert=1 ;;
    --email) email="${2:?}"; shift ;;
    --no-start) start=0 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1" ;;
  esac
  shift
done

is_root || die "нужен root (sudo)"
[ -r /etc/os-release ] && . /etc/os-release || true
case "${ID:-}${ID_LIKE:-}" in
  *ubuntu*|*debian*) ;;
  *) warn "система ${PRETTY_NAME:-неизвестна}: сценарий рассчитан на Ubuntu/Debian" ;;
esac

# ---------- 1. пакеты (только недостающие)
missing=()
for pkg in git curl ca-certificates python3 python3-venv rsync sqlite3 openssl; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
  log "ставлю пакеты: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
fi
command -v nginx >/dev/null 2>&1 || die "nginx не найден: этот сценарий свой nginx не ставит"
[ "$(python3 -c 'import sys; print(sys.version_info >= (3, 12))')" = "True" ] \
  || die "нужен системный Python 3.12 или новее, сейчас $(python3 -V)"

# uv: через pip в отдельное окружение, без скачивания установщиков из сети
if ! command -v uv >/dev/null 2>&1; then
  log "ставлю uv в /opt/uv-tool"
  python3 -m venv /opt/uv-tool
  /opt/uv-tool/bin/pip install --quiet --upgrade pip uv
  ln -sf /opt/uv-tool/bin/uv /usr/local/bin/uv
fi

# ---------- 2. пользователь и папки
if ! id "$APP_USER" >/dev/null 2>&1; then
  log "создаю пользователя $APP_USER"
  useradd --system --home-dir "/var/lib/$APP_USER" --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR" /var/backups/rabota /var/log/rabota /var/www/acme
chown "$APP_USER:$APP_USER" "$APP_DIR" /var/log/rabota
chmod 755 "$APP_DIR"
chmod 750 /var/log/rabota
chown "$APP_USER:$APP_USER" /var/backups/rabota; chmod 700 /var/backups/rabota
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data"
install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/media"

# ---------- 3. код и зависимости
if [ ! -d "$APP_DIR/.git" ]; then
  [ -z "$(ls -A "$APP_DIR" 2>/dev/null | grep -v -e '^data$' -e '^media$' || true)" ] || die "$APP_DIR не пуст и не репозиторий"
  log "клонирую $REPO ($BRANCH)"
  as_app git -C "$APP_DIR" init -q -b "$BRANCH"
  as_app git -C "$APP_DIR" remote add origin "$REPO"
  as_app git -C "$APP_DIR" fetch -q --depth 50 origin "$BRANCH"
  as_app git -C "$APP_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  log "репозиторий уже есть, пропускаю клонирование"
fi
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data"
as_app env UV_CACHE_DIR="$APP_DIR/.uv-cache" UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev --project "$APP_DIR"

# ---------- 4. .env
if [ ! -f "$APP_DIR/.env" ]; then
  log "создаю .env из образца"
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/.env.example" "$APP_DIR/.env"
  secret="$(openssl rand -hex 32)"; token="$(openssl rand -hex 24)"
  as_app "$APP_DIR/.venv/bin/python" - "$APP_DIR/.env" "$secret" "$token" <<'PY'
import re, sys
path, secret, token = sys.argv[1:4]
want = {"ENVIRONMENT": "production", "LOG_FORMAT": "json", "WEB_TRUST_PROXY": "1",
        "SECRET_KEY": secret, "HEALTH_TOKEN": token}
text = open(path, encoding="utf-8").read()
for key, value in want.items():
    pattern = re.compile(rf"^{key}=.*$", re.M)
    text = pattern.sub(f"{key}={value}", text) if pattern.search(text) else text.rstrip("\n") + f"\n{key}={value}\n"
open(path, "w", encoding="utf-8").write(text)
PY
  log ".env создан (SECRET_KEY и HEALTH_TOKEN сгенерированы, права 600). Заполните остальное: docs/DEPLOY.md"
else
  log ".env уже есть, не трогаю"
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"

# ---------- 5. юниты, logrotate
install_units
rc=0; install_rendered "$APP_DIR/deploy/logrotate-rabota" /etc/logrotate.d/rabota 644 || rc=$?
systemctl enable rabota-web.service rabota-worker.service rabota-backup.timer rabota-alert.timer >/dev/null 2>&1
if [ "$start" -eq 1 ]; then
  systemctl start rabota-web.service rabota-worker.service rabota-backup.timer rabota-alert.timer
  wait_healthy 40 && log "приложение отвечает на $LOCAL_URL/healthz" || warn "приложение не ответило за 40 секунд, смотрите journalctl -u rabota-web"
fi

# ---------- 6. nginx: только свой vhost
rc=0; install_nginx || rc=$?
[ "$rc" -eq 1 ] && die "vhost nginx не установлен, чужие сайты не затронуты (nginx -t отклонил конфигурацию)"

# ---------- 7. сертификат
if [ "$want_cert" -eq 1 ]; then
  command -v certbot >/dev/null 2>&1 || die "certbot не найден"
  contact=(--register-unsafely-without-email)
  [ -n "$email" ] && contact=(--email "$email")
  certbot certonly --webroot -w /var/www/acme --non-interactive --agree-tos "${contact[@]}" \
    -d "$DOMAIN" -d "www.$DOMAIN" --keep-until-expiring
  "$APP_DIR/scripts/xray-cert-add.sh" "$DOMAIN"
else
  cat <<MSG

Сертификат не выпускался. Когда DNS указывает на сервер:
  sudo certbot certonly --webroot -w /var/www/acme -d $DOMAIN -d www.$DOMAIN
  sudo $APP_DIR/scripts/xray-cert-add.sh $DOMAIN
MSG
fi

cat <<MSG

Готово. Дальше:
  1. Заполните $APP_DIR/.env (SITE_URL, SMTP, ключи, TELEGRAM_PROXY, данные оператора).
  2. sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/scripts/preflight.py
  3. sudo systemctl restart rabota-web rabota-worker
  4. Первый администратор: sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/scripts/create_admin.py почта@домен
  5. $APP_DIR/scripts/smoke.sh https://$DOMAIN
MSG
