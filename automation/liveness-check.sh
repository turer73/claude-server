#!/bin/bash
# META-MONITOR ("bekçileri kim bekliyor") — canlı-sistemin kendi sağlığı.
#
# liveness.check_all() -> ölü (dead) component varsa AKTİF uyar. KRİTİK tasarım: uyarı
# DIRECT Telegram (dead-man's switch) — çünkü ölü olan şey ALARM-PİPELİNE'ın kendisi
# (notify-cron) olabilir; o zaman spine'a yazmak işe yaramaz. Ayrıca spine'a da kaydet
# (notify-cron canlıysa buton+SessionStart; değilse en azından events'te iz kalır).
#
# Edge-detection: aynı dead-set tekrar alarm basmaz (spam yok). Cron: */10.
# NOT: bu script de cron'a bağlı; CRON/server tümüyle ölürse harici katman (uptime-kuma/
# VPS) yakalamalı — bu meta-monitor "component-ölü ama cron+server canlı" katmanını kapar.

APP_DIR="${LIVENESS_APP_DIR:-/opt/linux-ai-server}"
ENV_FILE="${NOTIFY_ENV_FILE:-$APP_DIR/.env}"
[ -f "$ENV_FILE" ] && { set -a; source "$ENV_FILE"; set +a; }
EMIT="${EMIT_EVENT:-$APP_DIR/scripts/emit-event.sh}"
STATE="${LIVENESS_STATE:-$APP_DIR/data/hook-state/liveness-dead-set}"
# klipper #100224: deploy-grace debounce. Tek-run dead (restart sırasında anlık) ile
# kalıcı-dead ayrımı: PENDING = önceki run'un dead-set'i; kaynak ikisi de dead = persistent.
PENDING="${STATE}.pending"
LOG="${LIVENESS_LOG:-/var/log/linux-ai-server/liveness-check.log}"
TG_URL="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
# LIVENESS_DEBOUNCE=2: N ardışık run (her */10dk = 20dk) dead olmadan alert basma.
# Override: LIVENESS_DEBOUNCE=1 (env-var) -> eski davranış (debounce yok).
LIVENESS_DEBOUNCE="${LIVENESS_DEBOUNCE:-2}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")" 2>/dev/null
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# check_all sonucu: test için LIVENESS_RESULT enjekte edilebilir; yoksa canlı çalıştır.
if [ -n "$LIVENESS_RESULT" ]; then
    RESULT="$LIVENESS_RESULT"
else
    RESULT=$(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -c \
        "import json; from app.core import liveness; print(json.dumps(liveness.check_all()))" 2>/dev/null)
fi
if [ -z "$RESULT" ]; then
    echo "[$TS] check_all çalışmadı/boş" >> "$LOG"
    echo "OUTCOME: fail | liveness check_all çalışmadı"
    exit 0
fi

DEAD=$(printf '%s' "$RESULT" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(' '.join(sorted(x['source'] for x in d.get('dead',[]))))" \
    2>/dev/null)
PREV=$(cat "$STATE" 2>/dev/null || echo "")

if [ -z "$DEAD" ]; then
    [ -n "$PREV" ] && echo "[$TS] RECOVERED — tüm bekçiler canlı (önceki alert: $PREV)" >> "$LOG"
    : > "$STATE" 2>/dev/null
    : > "$PENDING" 2>/dev/null
    echo "OUTCOME: pass | tüm bekçiler canlı"
    exit 0
fi

# Dead var. Debounce: kaynak ÖNCEKI run'da da dead mıydı?
# PENDING = geçen run'da dead olan set. Bu run'da da dead olan = persistent.
PREV_PENDING=$(cat "$PENDING" 2>/dev/null || echo "")
# Bu run'un dead-set'ini PENDING'e yaz (sonraki run okur)
echo "$DEAD" > "$PENDING" 2>/dev/null

if [ "${LIVENESS_DEBOUNCE}" -gt 1 ]; then
    # Kalıcı-dead = bu run'da VE geçen run'da dead olan kaynakların kesişimi
    PERSISTENT_DEAD=""
    for src in $DEAD; do
        echo "$PREV_PENDING" | grep -qw "$src" && PERSISTENT_DEAD="$PERSISTENT_DEAD $src"
    done
    PERSISTENT_DEAD="${PERSISTENT_DEAD# }"

    if [ -z "$PERSISTENT_DEAD" ]; then
        # Tek-run dead: deploy/restart anlık geçiş olabilir — bekle (debounce tur-1)
        echo "[$TS] debounce-1: dead=$DEAD (önceki=$PREV_PENDING; kalıcı değil, alarm yok)" >> "$LOG"
        echo "OUTCOME: pass | debounce-1: $DEAD (bir sonraki run kontrol)"
        exit 0
    fi
    DEAD_TO_ALERT="$PERSISTENT_DEAD"
else
    DEAD_TO_ALERT="$DEAD"
fi

# Edge: aynı alerted-set tekrar alarm basmaz (debounce sonrası: PERSISTENT hâlâ aynıysa suppress).
if [ "$DEAD_TO_ALERT" = "$PREV" ]; then
    echo "[$TS] dead sürüyor (tekrar-alarm yok): $DEAD_TO_ALERT" >> "$LOG"
    echo "OUTCOME: partial | dead sürüyor: $DEAD_TO_ALERT"
    exit 0
fi
echo "[$TS] YENİ PERSISTENT DEAD: $DEAD_TO_ALERT (tüm-dead: $DEAD)" >> "$LOG"

MSG="🛑 META-MONITOR: canlı-sistem bekçisi ÖLÜ
Dead: ${DEAD_TO_ALERT}
(DIRECT uyarı — alarm-pipeline'ın kendisi ölü olabilir, spine'a güvenilmedi.)
${TS}"

# 1) DIRECT Telegram (dead-man's switch — spine BYPASS, garantili kanal)
TG_OK=0
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 -X POST "$TG_URL" \
        -d chat_id="$TELEGRAM_CHAT_ID" --data-urlencode "text=${MSG}" 2>/dev/null)
    [ "$HTTP" = "200" ] && TG_OK=1
fi
# 2) Spine kaydı (notify-cron canlıysa buton+SessionStart; değilse events'te iz)
"$EMIT" alert "meta-monitor" critical "Canlı-sistem bekçisi ÖLÜ: ${DEAD_TO_ALERT}" \
    "liveness.check_all dead component; DIRECT-Telegram ile uyarıldı." 2>/dev/null || true

# State'i YALNIZ DIRECT alarm BAŞARIYLA iletildiyse yaz (Codex P1). Aksi halde (send-fail
# VEYA creds-yok) state YAZMA -> sonraki run hem direct hem spine'ı tekrar dener; dead-man's
# switch kaybolmaz. Degrade (creds-yok) bilinçle GÜRÜLTÜLÜ: ölü-bekçi sessizce yutulmasın.
if [ "$TG_OK" = "1" ]; then
    echo "$DEAD_TO_ALERT" > "$STATE" 2>/dev/null
else
    echo "[$TS] DIRECT-Telegram iletilmedi (http=${HTTP:-creds-yok}) -> state yazılmadı, retry" >> "$LOG"
fi

echo "OUTCOME: fail | dead bekçi: $DEAD_TO_ALERT (direct_tg=${TG_OK})"
exit 0
