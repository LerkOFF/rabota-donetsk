#!/usr/bin/env bash
# Проверка боевого сайта по адресу: коды ответа, заголовок страницы, robots, sitemap, healthz, заголовки безопасности.
#
#   scripts/smoke.sh [https://xn----7sbadrg9awfdp1a2d.xn--p1ai]
#
# Переменные:
#   SMOKE_RESOLVE=host:443:IP   проверить до смены DNS (передаётся в curl --resolve; для http нужен ещё вариант host:80:IP)
#   SMOKE_DEEP=strict|warn|skip /healthz/deep: strict (по умолчанию) требует status ok или degraded,
#                               warn только сообщает, skip не проверяет (сразу после установки воркер ещё не отработал)
#   SMOKE_INSECURE=1            не проверять сертификат (только для проверки по IP)
# Код возврата 1, если хоть одна проверка не прошла.
set -uo pipefail

BASE="${1:-https://xn----7sbadrg9awfdp1a2d.xn--p1ai}"
BASE="${BASE%/}"
HOST="${BASE#*://}"; HOST="${HOST%%/*}"
SCHEME="${BASE%%://*}"
DEEP="${SMOKE_DEEP:-strict}"

curl_opts=(-s --max-time 20 -A "rabota-smoke/1")
[ -n "${SMOKE_RESOLVE:-}" ] && curl_opts+=(--resolve "$SMOKE_RESOLVE")
[ "${SMOKE_INSECURE:-0}" = "1" ] && curl_opts+=(-k)

fails=0
pass() { printf 'ok     %s\n' "$1"; }
fail() { printf 'ОШИБКА %s\n' "$1"; fails=$((fails + 1)); }

code_of() { curl "${curl_opts[@]}" -o /dev/null -w '%{http_code}' "$@"; }
headers_of() { curl "${curl_opts[@]}" -D - -o /dev/null "$@" | tr -d '\r'; }
header() { printf '%s\n' "$1" | grep -i "^$2:" | head -n 1 | cut -d: -f2- | sed 's/^ *//'; }

expect_code() {
  local path="$1" want="$2" got
  got="$(code_of "$BASE$path")"
  if [ "$got" = "$want" ]; then pass "$path -> $got"; else fail "$path -> $got, ожидалось $want"; fi
}

# страницы
for path in / /vacancies /employers /about /login /register /legal/privacy; do expect_code "$path" 200; done
expect_code /vacancy/999999999 404
expect_code /no-such-page-for-smoke 404

# содержимое главной
body="$(curl "${curl_opts[@]}" "$BASE/")"
title="$(printf '%s' "$body" | tr '\n' ' ' | sed -n 's/.*<title>\([^<]*\)<\/title>.*/\1/p')"
if [ -n "$title" ]; then pass "title главной: $title"; else fail "на главной нет <title>"; fi
printf '%s' "$body" | grep -q '<link rel="canonical"' && pass "canonical на главной" || fail "на главной нет canonical"

# robots и sitemap
robots="$(curl "${curl_opts[@]}" "$BASE/robots.txt")"
printf '%s' "$robots" | grep -qi '^sitemap:' && pass "robots.txt содержит Sitemap" || fail "в robots.txt нет Sitemap"
printf '%s' "$robots" | grep -qi '^user-agent:' && pass "robots.txt содержит User-agent" || fail "в robots.txt нет User-agent"
sitemap="$(curl "${curl_opts[@]}" "$BASE/sitemap.xml")"
printf '%s' "$sitemap" | grep -q '<urlset' && printf '%s' "$sitemap" | grep -q '<loc>' \
  && pass "sitemap.xml с адресами" || fail "sitemap.xml пуст или не XML"

# health
hz="$(curl "${curl_opts[@]}" "$BASE/healthz")"
[ "$hz" = "ok" ] && pass "/healthz -> ok" || fail "/healthz вернул '$hz'"
if [ "$DEEP" != "skip" ]; then
  deep_code="$(code_of "$BASE/healthz/deep")"
  deep_body="$(curl "${curl_opts[@]}" "$BASE/healthz/deep")"
  deep_status="$(printf '%s' "$deep_body" | sed -n 's/.*"status": *"\([a-z]*\)".*/\1/p')"
  if [ "$deep_code" = "200" ]; then
    pass "/healthz/deep -> $deep_status"
  elif [ "$DEEP" = "warn" ]; then
    printf 'внимание /healthz/deep -> %s (%s), проверка в режиме warn\n' "$deep_code" "${deep_status:-нет ответа}"
  else
    fail "/healthz/deep -> $deep_code (${deep_status:-нет ответа})"
  fi
fi

# заголовки безопасности и сжатие
h="$(headers_of "$BASE/")"
if [ "$SCHEME" = "https" ]; then
  [ -n "$(header "$h" strict-transport-security)" ] && pass "HSTS" || fail "нет Strict-Transport-Security"
fi
for name in content-security-policy x-content-type-options x-frame-options referrer-policy permissions-policy; do
  [ -n "$(header "$h" "$name")" ] && pass "заголовок $name" || fail "нет заголовка $name"
done
[ "$(header "$h" x-content-type-options)" = "nosniff" ] || fail "X-Content-Type-Options не nosniff"
gz="$(curl "${curl_opts[@]}" -H 'Accept-Encoding: gzip' -D - -o /dev/null "$BASE/" | tr -d '\r' | grep -i '^content-encoding:' || true)"
[ -n "$gz" ] && pass "сжатие ответов: $(printf '%s' "$gz" | cut -d: -f2- | tr -d ' ')" || fail "ответ не сжимается (нет Content-Encoding)"

# статика
css_path="$(printf '%s' "$body" | grep -o '/static/tokens.css[^"]*' | head -n 1)"
if [ -n "$css_path" ]; then
  sh="$(headers_of "$BASE$css_path")"
  [ "$(printf '%s' "$sh" | head -n 1 | awk '{print $2}')" = "200" ] && pass "статика $css_path" || fail "статика $css_path не отдаётся"
  cc="$(header "$sh" cache-control)"
  case "$cc" in *max-age=*) pass "кеш статики: $cc" ;; *) fail "у статики нет Cache-Control max-age" ;; esac
else
  fail "на главной не найдена ссылка на tokens.css"
fi

# закрытое
for path in /admin /admin/; do
  got="$(code_of "$BASE$path")"
  case "$got" in 200) fail "$path открыт без входа (200)" ;; *) pass "$path закрыт ($got)" ;; esac
done
for path in /.env /.git/config /data/rabota.db; do
  got="$(code_of "$BASE$path")"
  case "$got" in 200) fail "$path отдаётся наружу" ;; *) pass "$path не отдаётся ($got)" ;; esac
done

# редиректы http -> https и www -> без www (только для боевого https-адреса)
if [ "$SCHEME" = "https" ]; then
  r="$(curl "${curl_opts[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "http://$HOST/")"
  case "$r" in "301 https://$HOST/"*) pass "http -> https: $r" ;; *) fail "http не уходит на https: $r" ;; esac
  r="$(curl "${curl_opts[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "https://www.$HOST/")"
  case "$r" in "301 https://$HOST/"*) pass "www -> без www: $r" ;; *) fail "www не уходит на основной адрес: $r" ;; esac
fi

if [ "$fails" -gt 0 ]; then
  printf '\nИтог: проверок не прошло %d.\n' "$fails"
  exit 1
fi
printf '\nИтог: все проверки прошли.\n'
