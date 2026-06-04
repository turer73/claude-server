#!/bin/bash
# App health watchdog -> events-spine.
#
# 2026-06-04 KONSOLIDASYON (tek-omurga): direkt-Telegram EMEKLİ. Sağlıksızlık artık
# events-spine'a yazılır (emit-event.sh) -> notify-cron -> Telegram. Kaynak
# 'service:linux-ai-server' => alert otomatik [🔧 Uygula]=systemctl restart butonu alır
# (Slice-2). Edge-detection (STATE) ile durum-değişiminde tek event.
API="${API:-http://localhost:8420}"
EMIT="${EMIT_EVENT:-/opt/linux-ai-server/scripts/emit-event.sh}"
LOG="${HEALTH_CHECK_LOG:-/var/log/linux-ai-server/health-check.log}"
STATE_FILE="${HEALTH_CHECK_STATE:-/tmp/health-check-state}"

ENV_FILE="${NOTIFY_ENV_FILE:-/opt/linux-ai-server/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

RESULT=$(curl -s --max-time 10 -X POST "$API/api/v1/monitor/webhooks/trigger/health_check" -H 'Content-Type: application/json' -H "X-API-Key: ${INTERNAL_API_KEY:-MISSING}")
HEALTHY=$(echo "$RESULT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("healthy",False))' 2>/dev/null)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PREV_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

if [ "$HEALTHY" = "True" ]; then
    echo "[$TIMESTAMP] Health OK" >> "$LOG" 2>/dev/null
    # Düzelme: log (notify-cron warn/critical-only -> info-recovery spam yok).
    if [ "$PREV_STATE" = "unhealthy" ]; then
        echo "[$TIMESTAMP] RECOVERED — servis tekrar sağlıklı" >> "$LOG" 2>/dev/null
    fi
    echo "healthy" > "$STATE_FILE" 2>/dev/null
    echo "OUTCOME: pass | healthy"
else
    echo "[$TIMESTAMP] UNHEALTHY — $RESULT" >> "$LOG" 2>/dev/null
    # Edge: yalnız healthy->unhealthy geçişinde emit (spam yok). service:linux-ai-server
    # => notify-cron [🔧 Uygula]=systemctl restart linux-ai-server butonu sunar.
    if [ "$PREV_STATE" != "unhealthy" ]; then
        "$EMIT" alert "service:linux-ai-server" critical \
            "Health check UNHEALTHY: servis yanıt vermiyor/sağlıksız" \
            "/health başarısız. Yanıt: $(printf '%s' "$RESULT" | head -c 200)"
    fi
    echo "unhealthy" > "$STATE_FILE" 2>/dev/null
    echo "OUTCOME: fail | unhealthy -> spine"
fi
exit 0
