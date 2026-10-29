#!/usr/bin/env bash
# Восстановление базы из копии.
#
#   scripts/restore.sh КОПИЯ.db.gz --yes            заменить боевую базу (службы будут остановлены и запущены)
#   scripts/restore.sh КОПИЯ.db.gz --target ФАЙЛ    развернуть в другой файл, ничего не останавливая (для проверки)
#
# Текущая база не удаляется: она переименовывается в <база>.before-restore-<время>.
# Порядок для сервера: проверить копию (scripts/verify-backup.sh), восстановить, проверить сайт (scripts/smoke.sh).
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=scripts/lib-sqlite.sh
source "$APP_DIR/scripts/lib-sqlite.sh"

DB_PATH="${DB_PATH:-$APP_DIR/data/rabota.db}"
SERVICES="${RESTORE_SERVICES:-rabota-web rabota-worker}"
backup="" target="" yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1; shift ;;
    --target) target="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) backup="$1"; shift ;;
  esac
done
[ -n "$backup" ] && [ -s "$backup" ] || die "укажите файл копии (.db.gz)"
case "$backup" in *.gpg) die "копия зашифрована: сначала расшифруйте gpg --decrypt" ;; esac

dest="${target:-$DB_PATH}"
if [ -z "$target" ] && [ "$yes" -ne 1 ]; then
  die "это заменит боевую базу $DB_PATH; добавьте --yes, если уверены"
fi

tmp="$dest.restore-tmp"
trap 'rm -f "$tmp"' EXIT
mkdir -p "$(dirname "$dest")"
gzip -dc "$backup" > "$tmp" || die "архив повреждён"
sqlite_ok "$tmp" || die "копия не прошла проверку целостности, база не тронута"

stopped=()
if [ -z "$target" ] && command -v systemctl >/dev/null 2>&1; then
  for svc in $SERVICES; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
      systemctl stop "$svc"; stopped+=("$svc")
    fi
  done
fi

if [ -e "$dest" ]; then
  mv "$dest" "$dest.before-restore-$(date +%Y%m%d-%H%M%S)"
fi
rm -f "$dest-wal" "$dest-shm"
mv "$tmp" "$dest"
if [ -z "$target" ]; then
  owner="$(stat -c '%U:%G' "$(dirname "$dest")" 2>/dev/null || true)"
  [ -n "$owner" ] && chown "$owner" "$dest" 2>/dev/null || true
fi
log "база восстановлена: $dest (вакансий $(sqlite_query "$dest" 'SELECT COUNT(*) FROM vacancies;'))"

for svc in ${stopped[@]+"${stopped[@]}"}; do
  systemctl start "$svc"
  log "запущена служба $svc"
done
