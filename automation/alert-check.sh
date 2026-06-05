#!/bin/bash
# Monitoring-pipeline liveness watchdog -> events-spine.
#
# 2026-06-04 KONSOLIDASYON (tek-omurga): metrik eşik-alarmı + n8n self-healing-01
# EMEKLİ edildi. Gerekçe: devops_agent (in-app, 30s tick, eşik cpu85/mem85/disk90/
# temp80 = eşit/daha-sıkı) cpu/mem/disk/temp + service + docker'ı ZATEN algılayıp
# events-spine'a emit ediyor -> notify-cron -> Telegram [✅ Gördüm]/[🔧 Uygula].
# İki ayrı yığında (alert-check->n8n + devops) aynı metriği izlemek çift-alarmdı.
#
# Bu script artık YALNIZ devops'un kendi-raporlayamayacağını izler: /metrics
# endpoint'inin kendisi erişilemezse (app down / INTERNAL_API_KEY rotate) devops da
# susar -> bağımsız cron-watchdog spine'a critical basar. Edge-detection (STATE) ile
# durum-değişiminde tek event (her run spam yok). Routing: emit-event.sh (TEK omurga).

API="${API:-http://localhost:8420}"
EMIT="${EMIT_EVENT:-/opt/linux-ai-server/scripts/emit-event.sh}"
STATE_FILE="${ALERT_CHECK_STATE:-/tmp/alert-check-monitoring-state}"
LOG="${ALERT_CHECK_LOG:-/var/log/linux-ai-server/alerts.log}"

# .env'den oku (test: NOTIFY_ENV_FILE=/dev/null ile izole)
ENV_FILE="${NOTIFY_ENV_FILE:-/opt/linux-ai-server/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PREV_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

# HEARTBEAT (her run, koşulsuz): liveness.alerts_evaluator alerts.log mtime tazeliğini
# alert-pipeline canlılığı sayar (>15dk=ölü). Konsolidasyon-sonrası eski "her run OK
# yaz" davranışı korunmalı yoksa pipeline yanlış-ölü görünür (regresyon fix).
echo "[$TIMESTAMP] alert-check run (heartbeat; metrik-alarm sahibi: devops_agent)" >> "$LOG" 2>/dev/null

# /metrics auth ŞART (admin scope = INTERNAL_API_KEY). http!=200 -> pipeline KÖR.
RESP=$(curl -s --max-time 10 -w "\n%{http_code}" -H "X-API-Key: ${INTERNAL_API_KEY}" "$API/api/v1/monitor/metrics")
HTTP_CODE=$(printf '%s' "$RESP" | tail -n1)

if [ "$HTTP_CODE" != "200" ]; then
    # Edge: yalnız up->down geçişinde emit (her 5dk run'da tekrar basma).
    if [ "$PREV_STATE" != "down" ]; then
        "$EMIT" alert "monitoring" critical \
            "Monitoring KÖR: /metrics http=${HTTP_CODE}" \
            "INTERNAL_API_KEY eksik/rotate? API down? devops_agent metrik-alarm emit edemez — bağımsız watchdog uyarısı."
        echo "[$TIMESTAMP] MONITORING_DOWN http=${HTTP_CODE} -> spine event" >> "$LOG" 2>/dev/null
    fi
    echo "down" > "$STATE_FILE" 2>/dev/null
    echo "OUTCOME: fail | metrics http=${HTTP_CODE} (pipeline KÖR -> spine)"
    exit 0
fi

# 200: pipeline canlı. down->up geçişi: log (notify-cron warn/critical-only, info spam yok).
if [ "$PREV_STATE" = "down" ]; then
    echo "[$TIMESTAMP] MONITORING_RECOVERED /metrics 200" >> "$LOG" 2>/dev/null
fi
echo "up" > "$STATE_FILE" 2>/dev/null
echo "OUTCOME: pass | metrics alive (metrik-alarm sahibi: devops_agent)"
exit 0
