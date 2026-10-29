#!/usr/bin/env bash
# Добавить сертификат домена в Xray, который держит публичный порт 443 и терминирует TLS по SNI.
#
#   sudo scripts/xray-cert-add.sh xn----7sbadrg9awfdp1a2d.xn--p1ai [--dry-run]
#
# Что делает (повторный запуск безопасен, лишнего не дублирует):
#   1. копирует fullchain.pem и privkey.pem из /etc/letsencrypt/live/<домен>/ в папку сертификатов Xray;
#   2. кладёт рядом с config.json резервную копию config.json.bak-<время>;
#   3. добавляет пару файлов в список certificates нужного inbound (по тегу, по умолчанию vless-tcp);
#   4. проверяет JSON и конфигурацию командой xray -test;
#   5. добавляет блок в deploy-хук certbot, чтобы продлённый сертификат копировался автоматически;
#   6. перезапускает xray. Если проверка или перезапуск не удались, возвращает прежний config.json.
#
# Параметры по умолчанию можно менять переменными окружения:
#   XRAY_CONFIG=/usr/local/etc/xray/config.json   XRAY_CERTS_DIR=/usr/local/etc/xray/certs
#   XRAY_INBOUND_TAG=vless-tcp                      XRAY_BIN=xray            XRAY_SERVICE=xray
#   LE_LIVE_DIR=/etc/letsencrypt/live               XRAY_HOOK=/etc/letsencrypt/renewal-hooks/deploy/xray-certs.sh
#   XRAY_CERT_OWNER=nobody:nogroup                  XRAY_ALLOW_NONROOT=1 (только для проверки скрипта без root)
set -euo pipefail

CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"
CERTS_DIR="${XRAY_CERTS_DIR:-/usr/local/etc/xray/certs}"
TAG="${XRAY_INBOUND_TAG:-vless-tcp}"
XRAY="${XRAY_BIN:-xray}"
SERVICE="${XRAY_SERVICE:-xray}"
LIVE="${LE_LIVE_DIR:-/etc/letsencrypt/live}"
HOOK="${XRAY_HOOK:-/etc/letsencrypt/renewal-hooks/deploy/xray-certs.sh}"
OWNER="${XRAY_CERT_OWNER:-nobody:nogroup}"
PYTHON="${XRAY_PYTHON:-python3}"

domain="" dry=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "неизвестный параметр: $arg" >&2; exit 2 ;;
    *) domain="$arg" ;;
  esac
done
log() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[ -n "$domain" ] || die "укажите домен (punycode), например xn----7sbadrg9awfdp1a2d.xn--p1ai"
case "$domain" in *[!A-Za-z0-9.-]*) die "в имени домена допустимы латиница, цифры, точка и дефис" ;; esac
[ "$(id -u)" = 0 ] || [ -n "${XRAY_ALLOW_NONROOT:-}" ] || die "нужен root (sudo)"
[ -f "$CONFIG" ] || die "нет конфигурации Xray: $CONFIG"
src_full="$LIVE/$domain/fullchain.pem"; src_key="$LIVE/$domain/privkey.pem"
[ -f "$src_full" ] && [ -f "$src_key" ] || die "нет сертификата в $LIVE/$domain (сначала выпустите его через certbot)"

dst_full="$CERTS_DIR/$domain.fullchain.pem"; dst_key="$CERTS_DIR/$domain.privkey.pem"
stamp="$(date +%Y%m%d-%H%M%S)"
backup="$CONFIG.bak-$stamp"
new="$(mktemp "${CONFIG}.new.XXXXXX")"
trap 'rm -f "$new"' EXIT

copy_certs() {
  mkdir -p "$CERTS_DIR"
  install -m 644 "$src_full" "$dst_full"
  install -m 640 "$src_key" "$dst_key"
  if [ -z "${XRAY_ALLOW_NONROOT:-}" ]; then chown "$OWNER" "$dst_full" "$dst_key"; fi
}

# новый config.json во временном файле; код 0: изменён, 3: уже есть, остальное ошибка
render_config() {
  "$PYTHON" - "$CONFIG" "$new" "$TAG" "$dst_full" "$dst_key" <<'PY'
import json, sys
src, dst, tag, cert, key = sys.argv[1:6]
with open(src, encoding="utf-8") as f:
    cfg = json.load(f)
inbound = next((i for i in cfg.get("inbounds", []) if i.get("tag") == tag), None)
if inbound is None:
    sys.exit(f"в конфигурации нет inbound с тегом {tag}")
tls = inbound.setdefault("streamSettings", {}).setdefault("tlsSettings", {})
certs = tls.setdefault("certificates", [])
if any(c.get("certificateFile") == cert for c in certs):
    sys.exit(3)
certs.append({"certificateFile": cert, "keyFile": key})
with open(dst, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
}

xray_test() {
  # формат указан явно: временный файл называется config.json.new.XXXXXX, по такому имени xray формат не определяет
  "$XRAY" run -test -format json -config "$1" >/dev/null 2>&1 || "$XRAY" -test -format json -config "$1" >/dev/null 2>&1
}

hook_block() {
  cat <<BLOCK
# rabota-donetsk:$domain begin
if [ "\$(basename "\${RENEWED_LINEAGE:-}")" = "$domain" ]; then
  install -m 644 "\$RENEWED_LINEAGE/fullchain.pem" "$dst_full"
  install -m 640 "\$RENEWED_LINEAGE/privkey.pem" "$dst_key"
  chown $OWNER "$dst_full" "$dst_key"
fi
# rabota-donetsk:$domain end
BLOCK
}

install_hook() {
  if [ -f "$HOOK" ] && grep -q "rabota-donetsk:$domain begin" "$HOOK"; then
    log "хук certbot уже содержит блок для $domain"
    return 0
  fi
  mkdir -p "$(dirname "$HOOK")"
  if [ ! -f "$HOOK" ]; then
    { printf '#!/bin/sh\n'; hook_block; printf 'systemctl restart %s\n' "$SERVICE"; } > "$HOOK"
    chmod 755 "$HOOK"
    log "создан хук certbot: $HOOK"
    return 0
  fi
  cp -p "$HOOK" "$HOOK.bak-$stamp"
  hook_block > "$new.hook"
  # блок идёт перед последним перезапуском xray; если перезапуска в хуке нет, в конец
  "$PYTHON" - "$HOOK" "$new.hook" <<'PY'
import sys
path, block_path = sys.argv[1:3]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
block = open(block_path, encoding="utf-8").read()
idx = None
for i, line in enumerate(lines):
    if "systemctl" in line and "restart" in line and "xray" in line:
        idx = i
if lines and not lines[-1].endswith("\n"):
    lines[-1] += "\n"
if idx is None:
    lines.append(block)
else:
    lines.insert(idx, block)
open(path, "w", encoding="utf-8").write("".join(lines))
PY
  rm -f "$new.hook"
  log "блок для $domain добавлен в $HOOK (резервная копия $HOOK.bak-$stamp)"
}

if [ "$dry" -eq 1 ]; then
  rc=0; render_config || rc=$?
  case "$rc" in
    0) log "dry-run: в $TAG будет добавлена пара $dst_full / $dst_key"; "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$new" ;;
    3) log "dry-run: сертификат уже есть в конфигурации, менять нечего" ;;
    *) exit "$rc" ;;
  esac
  log "dry-run: сертификат будет скопирован в $CERTS_DIR, хук $HOOK обновлён, $SERVICE перезапущен"
  exit 0
fi

copy_certs
log "сертификат скопирован в $CERTS_DIR"

rc=0; render_config || rc=$?
if [ "$rc" -eq 3 ]; then
  log "в конфигурации Xray сертификат уже есть, config.json не меняю"
  install_hook
  log "перезапускаю $SERVICE, чтобы подхватить обновлённые файлы"
  systemctl restart "$SERVICE"
  systemctl is-active --quiet "$SERVICE" || die "$SERVICE не запустился, смотрите journalctl -u $SERVICE"
  exit 0
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

"$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$new" || die "получился некорректный JSON, config.json не тронут"
xray_test "$new" || die "xray -test отклонил новую конфигурацию, config.json не тронут"

cp -p "$CONFIG" "$backup"
log "резервная копия: $backup"
cat "$new" > "$CONFIG"

rollback() {
  log "ОТКАТ: возвращаю прежний config.json"
  cp -p "$backup" "$CONFIG"
  systemctl restart "$SERVICE" || true
}

if ! xray_test "$CONFIG"; then rollback; die "проверка итоговой конфигурации не прошла"; fi
install_hook
systemctl restart "$SERVICE" || { rollback; die "не удалось перезапустить $SERVICE"; }
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then rollback; die "$SERVICE не запустился после изменения"; fi

# мягкая проверка: какой сертификат отдаётся по SNI
if command -v openssl >/dev/null 2>&1; then
  subject="$(echo | timeout 10 openssl s_client -connect 127.0.0.1:443 -servername "$domain" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null || true)"
  [ -n "$subject" ] && log "сертификат по SNI $domain: $subject" || log "не удалось проверить сертификат по SNI, проверьте вручную: openssl s_client -connect $domain:443 -servername $domain"
fi
log "готово: $domain добавлен в Xray ($TAG)"
