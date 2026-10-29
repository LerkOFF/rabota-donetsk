#!/usr/bin/env bash
# Обновление сайта на сервере. Безопасно запускать повторно.
#
#   sudo scripts/deploy.sh [--force] [--no-pull] [--skip-nginx] [--smoke URL]
#
# Порядок: блокировка -> git (fast-forward) -> зависимости по uv.lock -> резервная копия базы ->
#          проверка настроек, дымовая проверка приложения и миграция схемы -> юниты systemd и vhost nginx ->
#          перезапуск служб -> ожидание /healthz. Если проверка или запуск не удались, код возвращается на прежний коммит.
# База при откате кода не откатывается автоматически (схема только дополняется); при необходимости:
#   scripts/restore.sh /var/backups/rabota/<копия>.db.gz --yes
#
#   --force        перезапустить службы, даже если код не изменился
#   --no-pull      не трогать git, использовать текущее состояние папки
#   --skip-nginx   не менять vhost nginx
#   --smoke URL    после запуска прогнать scripts/smoke.sh по адресу (SMOKE_DEEP=warn)
# Переменные: APP_USER (rabota), APP_DIR, DEPLOY_BRANCH (main), DEPLOY_RUN_TESTS=1 (прогнать тесты, если установлен pytest).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-deploy.sh
source "$SELF_DIR/lib-deploy.sh"

BRANCH="${DEPLOY_BRANCH:-main}"
force=0 pull=1 nginx_step=1 smoke_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --no-pull) pull=0 ;;
    --skip-nginx) nginx_step=0 ;;
    --smoke) smoke_url="${2:?нужен адрес}"; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1" ;;
  esac
  shift
done

# журнал развёртываний рядом с выводом
if [ -d /var/log/rabota ] && [ -w /var/log/rabota ]; then
  exec > >(tee -a /var/log/rabota/deploy.log) 2>&1
fi

LOCK="${DEPLOY_LOCK:-/run/lock/rabota-deploy.lock}"
exec 9>"$LOCK" || die "не удалось открыть блокировку $LOCK"
flock -n 9 || die "другое развёртывание уже идёт"

cd "$APP_DIR"
[ -f .env ] || die "нет $APP_DIR/.env: сначала scripts/bootstrap-server.sh"

sync_deps() {
  as_app env UV_CACHE_DIR="${UV_CACHE_DIR:-$APP_DIR/.uv-cache}" UV_PYTHON_PREFERENCE=only-system \
    uv sync --frozen --no-dev --project "$APP_DIR"
}

restart_services() {
  sc restart rabota-web rabota-worker
  sc enable --now rabota-backup.timer rabota-alert.timer  # таймеры не должны остаться выключенными после первой установки
}

prev="$(as_app git rev-parse HEAD)"
log "текущая версия: $(as_app git rev-parse --short HEAD)"

if [ "$pull" -eq 1 ]; then
  as_app git fetch --prune origin "$BRANCH"
  as_app git merge --ff-only "origin/$BRANCH" || die "fast-forward невозможен (локальные правки в $APP_DIR?)"
fi
new="$(as_app git rev-parse HEAD)"
changed=0; [ "$prev" != "$new" ] && changed=1
log "версия после обновления: $(as_app git rev-parse --short HEAD)$([ "$changed" -eq 0 ] && echo ' (без изменений)')"

rollback() {
  local reason="$1"
  warn "$reason"
  if [ "$changed" -eq 1 ]; then
    warn "откат кода на $(printf '%s' "$prev" | cut -c1-7)"
    as_app git reset --hard "$prev"
    sync_deps || warn "не удалось восстановить зависимости"
    restart_services || true
    if wait_healthy 40; then log "после отката сайт отвечает"; else warn "после отката сайт НЕ отвечает, смотрите journalctl -u rabota-web"; fi
  else
    warn "код не менялся, откатывать нечего: смотрите journalctl -u rabota-web -n 100"
  fi
  last="$(find "${BACKUP_DIR:-/var/backups/rabota}" -maxdepth 1 -name 'rabota-*.db.gz*' 2>/dev/null | sort | tail -n 1)"
  [ -n "$last" ] && warn "если нужно вернуть и базу: scripts/restore.sh $last --yes"
  exit 1
}

sync_deps || rollback "установка зависимостей не удалась"

# резервная копия перед миграцией
db="$(dotenv_get DB_PATH)"; db="${db:-$APP_DIR/data/rabota.db}"
if [ -f "$db" ] && { [ "$changed" -eq 1 ] || [ "$force" -eq 1 ]; }; then
  as_app env DB_PATH="$db" BACKUP_DIR="${BACKUP_DIR:-/var/backups/rabota}" "$APP_DIR/scripts/backup.sh" \
    || warn "резервная копия перед обновлением не сделана"
fi

# проверка настроек, сборка приложения на временной базе, миграция боевой схемы
as_app "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/preflight.py" --smoke --migrate || rollback "проверка перед запуском не прошла"

if [ "${DEPLOY_RUN_TESTS:-0}" = "1" ] && as_app "$APP_DIR/.venv/bin/python" -c 'import pytest' 2>/dev/null; then
  as_app "$APP_DIR/.venv/bin/python" -m pytest -q -x tests/deploy tests/web -k "security or healthz or consent or analytics" \
    || rollback "тесты не прошли"
fi

units_changed=0
if is_root; then
  install_units
  if [ "$nginx_step" -eq 1 ] && command -v nginx >/dev/null 2>&1; then
    rc=0; install_nginx || rc=$?
    [ "$rc" -eq 1 ] && warn "vhost nginx не обновлён, сайт работает со старой конфигурацией"
  fi
else
  warn "не root: юниты systemd и vhost nginx не обновлялись"
fi

if [ "$changed" -eq 1 ] || [ "$force" -eq 1 ] || ! sc is-active --quiet rabota-web; then
  restart_services
  wait_healthy 40 || rollback "после перезапуска /healthz не отвечает за 40 секунд"
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$LOCAL_URL/")" = "200" ] || rollback "главная страница не отдаётся"
  log "службы перезапущены, сайт отвечает"
else
  wait_healthy 10 || rollback "сайт не отвечает на /healthz"
  log "код не менялся, службы не перезапускались"
fi

for svc in rabota-web rabota-worker; do
  sc is-active --quiet "$svc" || warn "служба $svc не активна"
done

if [ -n "$smoke_url" ]; then
  SMOKE_DEEP=warn "$APP_DIR/scripts/smoke.sh" "$smoke_url" || warn "scripts/smoke.sh нашёл проблемы (сайт при этом работает)"
fi
log "готово: $(as_app git rev-parse --short HEAD)"
