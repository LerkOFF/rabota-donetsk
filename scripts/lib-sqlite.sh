#!/usr/bin/env bash
# Общие функции для резервных копий SQLite. Подключается через `source`, сам не запускается.
# Если на сервере есть sqlite3, используется он (.backup), иначе python3 и модуль sqlite3 (то же API резервной копии).

SQLITE_PYTHON="${SQLITE_PYTHON:-python3}"

# sqlite_snapshot ИСТОЧНИК ПРИЁМНИК: согласованная копия работающей базы (WAL учитывается)
sqlite_snapshot() {
  local src="$1" dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".timeout 10000" ".backup '$dst'"
  else
    "$SQLITE_PYTHON" - "$src" "$dst" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
a = sqlite3.connect(src, timeout=10)
b = sqlite3.connect(dst)
with b:
    a.backup(b)
a.close()
b.close()
PY
  fi
}

# sqlite_query БАЗА SQL: одно значение из запроса (первая колонка первой строки)
sqlite_query() {
  local db="$1" sql="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 -readonly "$db" "$sql"
  else
    "$SQLITE_PYTHON" - "$db" "$sql" <<'PY'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
row = conn.execute(sys.argv[2]).fetchone()
print("" if row is None else row[0])
PY
  fi
}

# sqlite_ok БАЗА: 0, если PRAGMA integrity_check вернул ok
sqlite_ok() {
  [ "$(sqlite_query "$1" 'PRAGMA integrity_check;' 2>/dev/null)" = "ok" ]
}

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ОШИБКА: $*" >&2; exit 1; }
