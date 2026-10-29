#!/usr/bin/env bash
# Проверка для внешнего мониторинга (Uptime Kuma, Zabbix, cron) по /healthz/deep. Печатает одну строку.
#
#   scripts/check-health.sh [https://xn----7sbadrg9awfdp1a2d.xn--p1ai] [--strict]
#
# Коды возврата как у плагинов Nagios: 0 OK, 1 WARNING (degraded: часть источников просрочена), 2 CRITICAL (fail или нет ответа).
# --strict: degraded тоже считать аварией (код 2).
# Если задан HEALTH_TOKEN, он отправляется заголовком X-Health-Token и в строку попадают названия групп с проблемами.
#
# Для UptimeRobot: монитор типа "HTTP(s)" на <адрес>/healthz/deep (ответ 503 при аварии), либо "Keyword" со словом "ok"
# в режиме "должно отсутствовать слово fail". Для Uptime Kuma: тип "HTTP(s) - Keyword", ключевое слово "status":"ok" или "degraded".
set -uo pipefail

BASE="https://xn----7sbadrg9awfdp1a2d.xn--p1ai"
strict=0
for arg in "$@"; do
  case "$arg" in
    --strict) strict=1 ;;
    http*) BASE="${arg%/}" ;;
  esac
done

opts=(-s --max-time 15 -w '\n%{http_code}')
[ -n "${HEALTH_TOKEN:-}" ] && opts+=(-H "X-Health-Token: $HEALTH_TOKEN")

out="$(curl "${opts[@]}" "$BASE/healthz/deep" 2>/dev/null)"
code="${out##*$'\n'}"
body="${out%$'\n'*}"
status="$(printf '%s' "$body" | sed -n 's/.*"status": *"\([a-z]*\)".*/\1/p')"

if [ -z "$status" ] || [ "$code" = "000" ]; then
  echo "CRITICAL - нет ответа от $BASE/healthz/deep (код $code)"
  exit 2
fi
case "$status" in
  ok) echo "OK - сайт и приём данных в порядке"; exit 0 ;;
  degraded)
    echo "WARNING - часть источников просрочена: $(printf '%s' "$body" | tr -d '\n ' | cut -c1-200)"
    [ "$strict" -eq 1 ] && exit 2
    exit 1 ;;
  *) echo "CRITICAL - status=$status (http $code): $(printf '%s' "$body" | tr -d '\n ' | cut -c1-200)"; exit 2 ;;
esac
