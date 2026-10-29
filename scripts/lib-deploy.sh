#!/usr/bin/env bash
# Общие функции развёртывания. Подключается через `source` из deploy.sh и bootstrap-server.sh.

APP_USER="${APP_USER:-rabota}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DOMAIN="${DOMAIN:-xn----7sbadrg9awfdp1a2d.xn--p1ai}"
UNITS_DIR="${UNITS_DIR:-/etc/systemd/system}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
NGINX_SITE="${NGINX_SITE:-site-rabota}"
LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8000}"
DEPLOY_SRC="${DEPLOY_SRC:-$APP_DIR/deploy}"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '%s ПРЕДУПРЕЖДЕНИЕ: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { printf '%s ОШИБКА: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

is_root() { [ "$(id -u)" = 0 ]; }

# as_app КОМАНДА...: выполнить от имени пользователя проекта
as_app() {
  if [ "$(id -un)" = "$APP_USER" ]; then
    "$@"
  elif is_root; then
    runuser -u "$APP_USER" -- "$@"
  else
    die "запустите от root или от пользователя $APP_USER"
  fi
}

# sc АРГУМЕНТЫ: systemctl (через sudo, если не root)
sc() {
  if is_root; then systemctl "$@"; else sudo -n systemctl "$@"; fi
}

# render ФАЙЛ: подставить имя пользователя и папку проекта вместо заглушек
render() {
  sed -e "s#<USER>#$APP_USER#g" -e "s#<PROJECT_DIR>#$APP_DIR#g" "$1"
}

# install_rendered ШАБЛОН ЦЕЛЬ [РЕЖИМ]: записать цель, если содержимое отличается. Код 0: изменено, 10: без изменений
install_rendered() {
  local src="$1" dst="$2" mode="${3:-644}" tmp
  [ -s "$src" ] || { warn "нет шаблона $src"; return 1; }
  tmp="$(mktemp)"
  render "$src" > "$tmp" && [ -s "$tmp" ] || { rm -f "$tmp"; warn "не удалось подготовить $dst"; return 1; }
  if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
    rm -f "$tmp"
    return 10
  fi
  [ -f "$dst" ] && cp -p "$dst" "$dst.bak-$(date +%Y%m%d-%H%M%S)"
  install -m "$mode" "$tmp" "$dst"
  rm -f "$tmp"
  return 0
}

# значение переменной из .env (разбор как у приложения, без выполнения файла)
dotenv_get() {
  local name="$1" py="$APP_DIR/.venv/bin/python"
  [ -x "$py" ] || { echo ""; return 0; }
  "$py" -c "import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get(sys.argv[2]) or '')" "$APP_DIR/.env" "$name" 2>/dev/null || true
}

# wait_healthy [секунды]: ждать, пока приложение отвечает на /healthz
wait_healthy() {
  local limit="${1:-40}" i
  for ((i = 0; i < limit; i++)); do
    if [ "$(curl -fsS --max-time 3 "$LOCAL_URL/healthz" 2>/dev/null || true)" = "ok" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

UNIT_FILES=(rabota-web.service rabota-worker.service rabota-backup.service rabota-backup.timer rabota-alert.service rabota-alert.timer)

# install_units: поставить юниты systemd; печатает "changed", если что-то изменилось
install_units() {
  local changed=0 unit rc
  for unit in "${UNIT_FILES[@]}"; do
    rc=0
    install_rendered "$DEPLOY_SRC/$unit" "$UNITS_DIR/$unit" 644 || rc=$?
    if [ "$rc" -eq 0 ]; then changed=1; log "обновлён юнит $unit"; elif [ "$rc" -ne 10 ]; then die "юнит $unit не установлен"; fi
  done
  if [ "$changed" -eq 1 ]; then sc daemon-reload; fi
  return 0
}

# install_nginx: положить vhost, проверить nginx -t, при ошибке вернуть прежний. Код 0 успех, 1 ошибка, 10 без изменений
install_nginx() {
  local dst="$NGINX_AVAILABLE/$NGINX_SITE" link="$NGINX_ENABLED/$NGINX_SITE" rc=0 backup=""
  [ -d "$NGINX_AVAILABLE" ] || { warn "нет $NGINX_AVAILABLE: nginx не установлен?"; return 1; }
  if [ -f "$dst" ]; then backup="$(mktemp)"; cp -p "$dst" "$backup"; fi
  install_rendered "$DEPLOY_SRC/nginx-rabota.conf" "$dst" 644 || rc=$?
  if [ "$rc" -eq 1 ]; then return 1; fi
  if [ "$rc" -eq 10 ] && [ -e "$link" ]; then rm -f "$backup"; return 10; fi
  [ -e "$link" ] || ln -s "$dst" "$link"
  if nginx -t >/dev/null 2>&1; then
    sc reload nginx
    log "nginx: конфигурация применена"
    rm -f "$backup"
    return 0
  fi
  warn "nginx -t отклонил конфигурацию, возвращаю прежнюю"
  nginx -t 2>&1 | sed 's/^/    /' >&2 || true
  if [ -n "$backup" ]; then cp -p "$backup" "$dst"; rm -f "$backup"; else rm -f "$dst" "$link"; fi
  return 1
}
