#!/bin/bash
# klipper-cron-wrap.sh — Cron komutlarini saran wrapper.
# rc!=0 -> klipper-event.sh + n8n self-healing webhook tetikleyici
# Payload: workflow template field'larina tam uyumlu (alert.severity, value vb)

set +e

# Cron PATH minimaldir — webhook auth icin .env'den WEBHOOK_SECRET'i yukle
[ -f /opt/linux-ai-server/.env ] && set -a && source /opt/linux-ai-server/.env && set +a

NAME="${1:-unknown-cron}"
shift

# LOG_DIR env-override edilebilir (test). Oluşturulamazsa (perm/CI) TEMP'e düş — yoksa
# alttaki 'sqlite3 ... 2>>$LOG' redirect-hedefi olmadığından sqlite3 HİÇ çalışmaz ve
# cron_outcomes INSERT SESSİZCE atlanır (gerçek SENSE-riski: /var/log dolarsa/yazılamazsa
# outcome-kaydı durur). Fallback ile cron_outcomes yazımı log-dizinine bağımlı olmaz.
LOG_DIR="${LOG_DIR:-/var/log/linux-ai-server}"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="$(mktemp -d 2>/dev/null || echo /tmp)"
LOG="${LOG_DIR}/${NAME}.log"

if [ $# -eq 0 ]; then
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "MISSING-COMMAND"
    exit 2
fi

CMD_STR="$*"
TS_START=$(date -Iseconds)
echo "[$TS_START] === START ${NAME}: ${CMD_STR} ===" >> "$LOG"

# Bu-run cikti'sini taze temp'e yakala (current-run-only OUTCOME scan): append'li
# $LOG'da onceki run'in marker'i yanlis-atfedilmesin. Sonra $LOG'a ekle.
RUN_OUT="$(mktemp "/tmp/cron-${NAME}.XXXXXX" 2>/dev/null || mktemp)"
"$@" > "$RUN_OUT" 2>&1
RC=$?
cat "$RUN_OUT" >> "$LOG"
TS_END=$(date -Iseconds)
echo "[$TS_END] === END ${NAME}: rc=${RC} ===" >> "$LOG"

# ── Outcome-contract (LIVESYS Faz 1): gercek sonucu rc'den degil, isin bastigi
# son `OUTCOME: <pass|partial|fail> | <detay>` marker'indan turet. ──
OUTCOME_LINE="$(grep -aE '^OUTCOME:[[:space:]]*(pass|partial|fail)' "$RUN_OUT" | tail -1)"
rm -f "$RUN_OUT"

if [ -n "$OUTCOME_LINE" ]; then
    RESULT="$(printf '%s' "$OUTCOME_LINE" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail).*/\1/')"
    DETAIL="$(printf '%s' "$OUTCOME_LINE" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail)[[:space:]]*\|?[[:space:]]*//')"
    SOURCE="predicate"
    # Sertlestirme #1: pass beyan ama rc!=0 = CELISKI (pass-yaz-sonra-crash/timeout) -> fail.
    if [ "$RESULT" = "pass" ] && [ "$RC" -ne 0 ]; then
        RESULT="fail"; SOURCE="outcome-rc-mismatch"; DETAIL="declared pass but rc=${RC}; ${DETAIL}"
    fi
else
    # Marker yok -> rc-fallback (eski davranisla birebir) + outcome-undefined (sessiz kapsam yok).
    if [ "$RC" -eq 0 ]; then RESULT="pass"; else RESULT="fail"; fi
    SOURCE="rc-fallback"; DETAIL="outcome-undefined (no OUTCOME marker)"
fi

# Merkezi kayit (server.db). Her run ayri satir (retry-pass fail'i EZMEZ). Kuma
# dead-man's-switch'i ("hic kosmadi") REPLACE etmez — tamamlayici.
DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
if [ -f "$DB_PATH" ]; then
    SAFE_DETAIL="$(printf '%s' "$DETAIL" | tr -d '\\`"' | tr '\n\r\t' '   ' | head -c 300)"
    SAFE_DETAIL="${SAFE_DETAIL//\'/\'\'}"  # SQL single-quote escape
    # .timeout 10000: WAL-contention (FastAPI 2-worker + diger yazicilar) aninda
    # SQLITE_BUSY(5) yerine 10sn lock bekle — aksi halde cron_outcomes INSERT
    # sessizce duser (#517: 19:15'ten beri yazilmiyordu). Fail artik LOG'a gorunur.
    sqlite3 -cmd ".timeout 10000" "$DB_PATH" \
        "INSERT INTO cron_outcomes (job,result,rc,source,detail) VALUES ('${NAME}','${RESULT}',${RC},'${SOURCE}','${SAFE_DETAIL}');" \
        2>>"$LOG" || echo "[$(date -Iseconds)] WARN cron_outcomes INSERT basarisiz (db busy/locked) job=${NAME}" >>"$LOG"
fi

# ── Alert: gercek RESULT'a gore (sadece rc!=0 degil) — partial de yuzeye cikar. ──
# CANARY_SUPPRESS_ALERT=1 (livesys-canary.sh): cron_outcomes ZATEN yazildi (yukarida);
# burada alert/event/notify ATLANIR — canary alarm-yolunu test ederken GERCEK alarm
# tetiklemez (sentetik known-bad spam yapmasin). Default 0 → davranis degismez.
if [ "${CANARY_SUPPRESS_ALERT:-0}" = "1" ]; then
    : # canary: cron_outcomes yeterli, alert/event yok
elif [ "$RESULT" = "pass" ]; then
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "OK"
else
    SEV="critical"; [ "$RESULT" = "partial" ] && SEV="warning"
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "${RESULT} rc=${RC} (${DETAIL})"

    # LIVESYS Faz 3.2: merkezi events kaydi (job-outcome). YALNIZCA kayit — bildirim
    # AYRI notify-cron'un isi (henuz yok), bu yuzden ustteki alert-POST hala tek-notifier
    # ve cift-bildirim YOK. emit-helper fail-safe (cron-job'u dusurmez). sev: warning->warn.
    /opt/linux-ai-server/scripts/emit-event.sh "job-outcome" "cron:${NAME}" "${SEV}" "cron ${NAME} ${RESULT}" "rc=${RC} ${DETAIL}"

    # ATOMIK CUTOVER: NOTIFY_CRON_ENABLED=true aninda notify-cron devralir, bu POST durur.
    # NOTIFY_CRON_ENABLED=false (default): legacy direkt n8n POST aktif (double-yok garanti icin
    # emit-event'in yazdigi son satiri hemen mark_notified yap — notify-cron enable'da cift-bildirim engeli).
    if [ "${NOTIFY_CRON_ENABLED:-false}" != "true" ]; then
        SAFE_CMD=$(printf "%s" "$CMD_STR" | tr -d '\\"`' | tr '\n\r\t' '   ' | head -c 200)
        SAFE_MSG=$(printf "%s" "$DETAIL" | tr -d '\\"`' | tr '\n\r\t' '   ' | head -c 160)
        BODY="{\"alert\":{\"source\":\"klipper-cron-${NAME}\",\"severity\":\"${SEV}\",\"message\":\"cron ${NAME} ${RESULT} rc=${RC} (${SAFE_MSG})\",\"value\":${RC},\"threshold\":0},\"meta\":{\"type\":\"cron_failure\",\"project\":\"klipper-cron\",\"device\":\"klipper\",\"command\":\"${SAFE_CMD}\",\"exit_code\":\"${RC}\",\"result\":\"${RESULT}\",\"outcome_source\":\"${SOURCE}\",\"auto_fix_eligible\":true,\"hook_source\":\"klipper-cron-wrap\"}}"
        curl -s -X POST --max-time 3 \
            -H "Content-Type: application/json" \
            -H "X-Webhook-Secret: ${WEBHOOK_SECRET:-MISSING}" \
            -d "${BODY}" \
            "http://localhost:5678/webhook/klipper-alert" > /dev/null 2>&1 || true
        # Mark just-emitted event notified — notify-cron enable'da cift-bildirim engeli
        if [ -f "$DB_PATH" ]; then
            sqlite3 -cmd ".timeout 10000" "$DB_PATH" \
                "UPDATE events SET notified=1 WHERE source='cron:${NAME}' AND notified=0 AND id=(SELECT id FROM events WHERE source='cron:${NAME}' AND notified=0 ORDER BY id DESC LIMIT 1);" \
                2>/dev/null || true
        fi
    fi
fi

exit $RC
