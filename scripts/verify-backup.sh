#!/usr/bin/env bash
# Проверка восстановления: берёт копию, разворачивает во временную папку и проверяет базу.
#
#   scripts/verify-backup.sh [файл.db.gz] [--max-age-hours N]
#
# Без файла берётся самая свежая копия из BACKUP_DIR. Проверяется: файл читается, PRAGMA integrity_check = ok,
# есть таблицы vacancies и users, копия не старше --max-age-hours (по умолчанию 36). Код возврата 1 при любой проблеме.
# Зашифрованные копии (.gpg) проверяются только по наличию и размеру: ключ на сервере не хранится.
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=scripts/lib-sqlite.sh
source "$APP_DIR/scripts/lib-sqlite.sh"

BACKUP_DIR="${BACKUP_DIR:-/var/backups/rabota}"
max_age=36
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --max-age-hours) max_age="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) file="$1"; shift ;;
  esac
done

if [ -z "$file" ]; then
  file="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'rabota-*.db.gz*' ! -name '*-files*' 2>/dev/null | sort | tail -n 1)"
  [ -n "$file" ] || die "в $BACKUP_DIR нет ни одной копии"
fi
[ -s "$file" ] || die "файл пуст или не найден: $file"

mtime="$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file")"
age_hours=$(( ( $(date +%s) - mtime ) / 3600 ))
if [ "$age_hours" -gt "$max_age" ]; then
  die "копия $file старше $max_age ч ($age_hours ч): резервное копирование не работает?"
fi

case "$file" in
  *.gpg) log "зашифрованная копия $file: размер $(du -h "$file" | cut -f1), возраст $age_hours ч (содержимое не проверялось)"; exit 0 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
gzip -dc "$file" > "$tmp/restored.db" || die "архив повреждён: $file"
sqlite_ok "$tmp/restored.db" || die "проверка целостности не пройдена: $file"
for table in vacancies users; do
  [ "$(sqlite_query "$tmp/restored.db" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table';")" = "1" ] \
    || die "в копии нет таблицы $table"
done
vac="$(sqlite_query "$tmp/restored.db" 'SELECT COUNT(*) FROM vacancies;')"
usr="$(sqlite_query "$tmp/restored.db" 'SELECT COUNT(*) FROM users;')"
log "копия в порядке: $file (возраст $age_hours ч, вакансий $vac, пользователей $usr)"
