#!/bin/bash
# ============================================
# Bilge Arena Failover — Coolify down → Vercel
# Klipper cron: */3 * * * * (her 3 dakikada kontrol)
# ============================================
# v2: Origin IP uzerinden kontrol (Cloudflare cache bypass)
#     Deploy sirasinda false positive onleme (cooldown)

source /opt/linux-ai-server/.env 2>/dev/null

# Config
ORIGIN_URL="http://194.163.134.239:3000/api/health/ping"
SITE_URL="https://www.bilgearena.com/api/health/ping"
VERCEL_CHECK="https://bilge-arena-gamma.vercel.app/api/health/ping"
CF_TOKEN="${CLOUDFLARE_API_TOKEN:-}"
CF_ZONE="${CLOUDFLARE_BILGE_ARENA_ZONE_ID:-bd201cce2ca524333cc7f13757501f89}"
CF_RECORD="${CLOUDFLARE_BILGE_ARENA_RECORD_ID:-ac5592a51f91872f99e4bddb2b2878dc}"
if [ -z "$CF_TOKEN" ]; then
    echo "ERROR: CLOUDFLARE_API_TOKEN is not set in .env" >&2
    exit 1
fi
COOLIFY_IP="194.163.134.239"
VERCEL_IP="76.76.21.21"
STATE_FILE="/tmp/bilge-arena-failover-state"
LOG="/var/log/linux-ai-server/failover.log"
MAX_FAILURES=3
COOLDOWN_FILE="/tmp/bilge-arena-failover-cooldown"
COOLDOWN_SECONDS=300  # 5 dk cooldown — recovery sonrasi hemen tekrar failover yapma

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

log() {
    echo "[$(date +%Y-%m-%d\ %H:%M:%S)] $1" >> "$LOG"
}

# Cooldown kontrolu — recovery sonrasi 5 dk bekle
if [ -f "$COOLDOWN_FILE" ]; then
    COOLDOWN_TIME=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    DIFF=$((NOW - COOLDOWN_TIME))
    if [ "$DIFF" -lt "$COOLDOWN_SECONDS" ]; then
        exit 0  # Cooldown suresi icinde, atla
    else
        rm -f "$COOLDOWN_FILE"
    fi
fi

# Mevcut state oku
CURRENT_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "coolify")
FAIL_COUNT=$(cat "${STATE_FILE}.failures" 2>/dev/null || echo "0")

# Health check — ONCE origin IP uzerinden (Cloudflare bypass)
# Caddy/container'a direkt baglanarak gercek durumu kontrol et
HTTP_ORIGIN=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 15 \
    -H "Host: www.bilgearena.com" "https://$COOLIFY_IP/api/health/ping" --insecure 2>/dev/null)

# Origin basarisizsa, Cloudflare uzerinden de kontrol et (ikili dogrulama)
if [ "$HTTP_ORIGIN" = "200" ]; then
    HTTP_CODE="200"
else
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 15 "$SITE_URL" 2>/dev/null)
fi

if [ "$HTTP_CODE" = "200" ]; then
    # Site calisiyor
    echo "0" > "${STATE_FILE}.failures"

    if [ "$CURRENT_STATE" = "vercel" ]; then
        # Coolify geri geldi — DNS'i geri al
        RESULT=$(curl -s -X PATCH "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/dns_records/$CF_RECORD" \
            -H "Authorization: Bearer $CF_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"$COOLIFY_IP\",\"proxied\":true}")

        SUCCESS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success',False))" 2>/dev/null)

        if [ "$SUCCESS" = "True" ]; then
            echo "coolify" > "$STATE_FILE"
            # Cooldown baslat — 5 dk boyunca tekrar failover yapma
            date +%s > "$COOLDOWN_FILE"
            log "RECOVERY: DNS Coolify'a geri alindi ($COOLIFY_IP) — 5dk cooldown"
            send_telegram "$(printf '🟢 *Bilge Arena RECOVERY*\nCoolify tekrar calisiyor!\nDNS Coolify a geri alindi.\nDowntime sonu: %s' "$(date +%H:%M)")"
        fi
    fi
else
    # Site down (origin + cloudflare ikisi de basarisiz)
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "$FAIL_COUNT" > "${STATE_FILE}.failures"
    log "DOWN: HTTP origin=$HTTP_ORIGIN cf=$HTTP_CODE (fail $FAIL_COUNT/$MAX_FAILURES)"

    if [ "$FAIL_COUNT" -ge "$MAX_FAILURES" ] && [ "$CURRENT_STATE" = "coolify" ]; then
        # Vercel alive mi kontrol et
        VERCEL_CODE=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 "$VERCEL_CHECK" 2>/dev/null)

        if [ "$VERCEL_CODE" = "200" ]; then
            # Failover: DNS'i Vercel'e cevir
            RESULT=$(curl -s -X PATCH "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/dns_records/$CF_RECORD" \
                -H "Authorization: Bearer $CF_TOKEN" \
                -H "Content-Type: application/json" \
                -d "{\"content\":\"$VERCEL_IP\",\"proxied\":true}")

            SUCCESS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success',False))" 2>/dev/null)

            if [ "$SUCCESS" = "True" ]; then
                echo "vercel" > "$STATE_FILE"
                log "FAILOVER: DNS Vercel'e cevirildi ($VERCEL_IP)"
                send_telegram "$(printf '🔴 *Bilge Arena FAILOVER*\nCoolify %d kez basarisiz!\nOrigin: HTTP %s | CF: HTTP %s\nDNS Vercel e cevirildi.\nFailover: %s' "$MAX_FAILURES" "$HTTP_ORIGIN" "$HTTP_CODE" "$(date +%H:%M)")"
            else
                log "FAILOVER FAILED: Cloudflare API hatasi"
                send_telegram "$(printf '🔴 *Bilge Arena DOWN — Failover BASARISIZ*\nCloudflare DNS degistirilemedi!\nManuel mudahale gerekli.')"
            fi
        else
            log "FAILOVER SKIP: Vercel da down (HTTP $VERCEL_CODE)"
            send_telegram "$(printf '🔴 *Bilge Arena DOWN*\nCoolify: HTTP %s\nVercel: HTTP %s\nHer iki platform da down!' "$HTTP_CODE" "$VERCEL_CODE")"
        fi
    fi
fi
