#!/usr/bin/env bash
# renderhane process-webhooks kuyruk drenajı (*/5 cron).
#
# NEDEN klipper'da: Vercel Hobby planı max 2 cron + sadece günlük frekans
# desteklediği için process-webhooks'un */5 cron'u vercel.json'dan çıkarıldı
# (PR turer73/renderhane#19). Bu script endpoint'i tetikleyerek pgmq webhook
# kuyruğunu boşaltır.
#
# Cron entry (klipper-cron-wrap.sh ile sarılır → OUTCOME:fail event+Telegram üretir):
#   */5 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh rh-process-webhooks \
#     /opt/linux-ai-server/automation/rh-process-webhooks.sh
#
# Güvenlik: CRON_SECRET .env'den okunur, geçici 600-perm curl-config'e yazılır
# (ps/crontab arg'larında PLAINTEXT GÖRÜNMEZ), curl --config ile kullanılır,
# çıkışta silinir. İlk sürüm (Sonnet 4.6, 082d7e1) secret'ı -H ile geçiyordu
# (ps-sızıntı) ve OUTCOME marker'ı yoktu → bu sürüm ikisini de düzeltir.
set -uo pipefail

ENV_FILE="${ENV_FILE:-/opt/linux-ai-server/.env}"
URL="${RH_WEBHOOK_URL:-https://www.renderhane.com/api/cron/process-webhooks}"

SECRET="$(grep -m1 '^RENDERHANE_CRON_SECRET=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r\n")"
if [ -z "${SECRET}" ]; then
  echo "OUTCOME: fail | RENDERHANE_CRON_SECRET .env'de yok/bos"
  exit 1
fi

CFG="$(mktemp)"; chmod 600 "$CFG"; trap 'rm -f "$CFG"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "$SECRET" > "$CFG"

BODY="$(curl -sS --max-time 90 -w $'\n__HTTP_%{http_code}__' --config "$CFG" "$URL" 2>&1)"
CODE="$(printf '%s' "$BODY" | sed -nE 's/.*__HTTP_([0-9]+)__$/\1/p')"
JSON="$(printf '%s' "$BODY" | sed -E 's/__HTTP_[0-9]+__$//' | tr -d '\n' | head -c 300)"

if [ "${CODE}" = "200" ]; then
  N="$(printf '%s' "$JSON" | sed -nE 's/.*"processed"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p')"
  echo "OUTCOME: pass | processed=${N:-0} http=200"
  exit 0
else
  echo "OUTCOME: fail | http=${CODE:-000} resp=$(printf '%s' "$JSON" | head -c 150)"
  exit 1
fi
