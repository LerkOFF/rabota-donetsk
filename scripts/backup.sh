#!/usr/bin/env bash
# Резервная копия базы SQLite: согласованный снимок работающей базы, проверка целостности, сжатие, ротация.
#
#   scripts/backup.sh
#
# Настройки (переменные окружения, обычно из .env через systemd):
#   DB_PATH                  база, по умолчанию <проект>/data/rabota.db
#   BACKUP_DIR               куда класть копии, по умолчанию /var/backups/rabota
#   BACKUP_KEEP_DAYS         сколько дней хранить, по умолчанию 14
#   BACKUP_GPG_RECIPIENT     если задан, копия шифруется открытым ключом gpg (файл .gpg)
#   BACKUP_RSYNC_TARGET      если задан, копия отправляется rsync (хост:папка) на другой сервер в России
# Рядом с базой копируются файлы, без которых её не восстановить по смыслу: сессия Telegram, список каналов,
# ключ подписи форм. Они лежат в отдельном архиве rabota-<время>-files.tar.gz.
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=scripts/lib-sqlite.sh
source "$APP_DIR/scripts/lib-sqlite.sh"

DB_PATH="${DB_PATH:-$APP_DIR/data/rabota.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rabota}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

[ -f "$DB_PATH" ] || die "база не найдена: $DB_PATH"
case "$KEEP_DAYS" in ''|*[!0-9]*) die "BACKUP_KEEP_DAYS должно быть числом" ;; esac
mkdir -p "$BACKUP_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
work="$(mktemp -d "$BACKUP_DIR/.work.XXXXXX")"
trap 'rm -rf "$work"' EXIT

snapshot="$work/rabota.db"
sqlite_snapshot "$DB_PATH" "$snapshot"
sqlite_ok "$snapshot" || die "снимок не прошёл проверку целостности, копия не сохранена"

out="$BACKUP_DIR/rabota-$stamp.db.gz"
gzip -c "$snapshot" > "$work/out.gz"
if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
  gpg --batch --yes --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
    --output "$work/out.gz.gpg" --encrypt "$work/out.gz"
  out="$out.gpg"
  mv "$work/out.gz.gpg" "$out"
else
  mv "$work/out.gz" "$out"
fi

# файлы рядом с базой (если есть)
data_dir="$(dirname "$DB_PATH")"
extras=()
for name in telegram.session channels.txt .secret_key; do
  [ -f "$data_dir/$name" ] && extras+=("$name")
done
if [ "${#extras[@]}" -gt 0 ]; then
  extra_out="$BACKUP_DIR/rabota-$stamp-files.tar.gz"
  tar -C "$data_dir" -czf "$work/files.tgz" "${extras[@]}"
  if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
    gpg --batch --yes --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
      --output "$extra_out.gpg" --encrypt "$work/files.tgz"
  else
    mv "$work/files.tgz" "$extra_out"
  fi
fi

# ротация: файлы старше KEEP_DAYS суток удаляются
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'rabota-*' -mtime "+$KEEP_DAYS" -delete

size="$(du -h "$out" | cut -f1)"
log "копия готова: $out ($size), хранится $KEEP_DAYS дн."

if [ -n "${BACKUP_RSYNC_TARGET:-}" ]; then
  rsync -a --chmod=F600 "$BACKUP_DIR"/rabota-"$stamp"* "$BACKUP_RSYNC_TARGET/" \
    && log "копия отправлена на $BACKUP_RSYNC_TARGET" \
    || log "ПРЕДУПРЕЖДЕНИЕ: отправка на $BACKUP_RSYNC_TARGET не удалась, локальная копия сохранена"
fi
