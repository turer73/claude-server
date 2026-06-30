#!/bin/bash
# notify-cron.sh — LIVESYS FAZ3.2 step-d: events tablosu pending -> Telegram bildirimi.
# Author: surer (draft) + klipper (cross-verify: obs-1/2/3, #99772) + Codex (#24 LIMIT/rc/OUTCOME)
#        2026-06-03: n8n backend -> direkt Telegram Bot API (n8n klipper'da workflow yok).
#
# DISABLED-first: .env'de NOTIFY_CRON_ENABLED=true ayarlanana kadar calismaz.
# Cadence: */20 (automation/crontab'da kayitli)
#
# ATOMIK CUTOVER: NOTIFY_CRON_ENABLED=true aninda klipper-cron-wrap direkt n8n POST
# + backup-monitor send_telegram durur; bu script devralir (DOUBLE-yok).
#
# SEND-THEN-MARK: mark_notified SADECE basarili HTTP 200 sonrasi -> at-least-once
# (fail -> mark-YOK -> sonraki run retry -> NO-LOSS).
set +e

_envget() { local _f="${NOTIFY_ENV_FILE:-/opt/linux-ai-server/.env}"; grep -E "^$1=" "$_f" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'"; }

# env-var override > .env (test/systemd env-var'i kazanir; cron .env'den okur).
NOTIFY_CRON_ENABLED="${NOTIFY_CRON_ENABLED:-$(_envget NOTIFY_CRON_ENABLED)}"
[ "${NOTIFY_CRON_ENABLED:-false}" = "true" ] || exit 0

DB_PATH="${DB_PATH:-$(_envget DB_PATH)}"; DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(_envget TELEGRAM_BOT_TOKEN)}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(_envget TELEGRAM_CHAT_ID)}"
MEMORY_API_KEY="${MEMORY_API_KEY:-$(_envget MEMORY_API_KEY)}"
API_BASE="${API_BASE:-http://localhost:8420}"
LOG="${NOTIFY_CRON_LOG:-/var/log/linux-ai-server/notify-cron.log}"
# Auto-bug yaşam-döngüsü: MEMORY_DB (discoveries), auto-resolve sessizlik-eşiği,
# tekrar-deseni penceresi/eşiği. (Slice A: kaynak QUIET dk sessiz=düzeldi -> resolve.
# Slice C: kaynağın son RECUR_DAYS'teki critical sayısı >= RECUR_THRESHOLD -> tekrarlayan.)
MEMORY_DB="${MEMORY_DB:-/opt/linux-ai-server/data/claude_memory.db}"
RESOLVE_QUIET_MIN="${RESOLVE_QUIET_MIN:-90}"
RECUR_DAYS="${RECUR_DAYS:-7}"
RECUR_THRESHOLD="${RECUR_THRESHOLD:-3}"
# Slice B (opt-in, default KAPALI): tekrarlayan-critical -> arka-planda otonom salt-okunur
# /claude incelemesi. FAZ6 kapı: yalnız enable + tekrarlayan + (script-içi) rate-limit.
AUTO_INVESTIGATE_ENABLED="${AUTO_INVESTIGATE_ENABLED:-$(_envget AUTO_INVESTIGATE_ENABLED)}"
INVESTIGATE_SCRIPT="${INVESTIGATE_SCRIPT:-$(dirname "$0")/../scripts/auto-investigate.py}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# DB-yok = notify-cron calisamaz; SESSIZ-pass DEGIL (Codex #24): OUTCOME:fail emit.
[ -f "$DB_PATH" ] || { echo "[$(date -Iseconds)] DB not found: $DB_PATH" >> "$LOG"; echo "OUTCOME: fail | DB yok: $DB_PATH"; exit 0; }

# Telegram opsiyonel-bağımsız (Codex P2): creds eksik olsa bile hata-hafızası
# (critical->discovery) yazılmalı ki SessionStart Telegram-down iken de açık-hatayı
# görsün. TG_OK=0 ise gönderim atlanır ama memory-kaydı sürer. İkisi de yoksa -> çık.
TG_OK=1
[ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ] && TG_OK=0
if [ "$TG_OK" = "0" ] && [ -z "$MEMORY_API_KEY" ]; then
    echo "[$(date -Iseconds)] TELEGRAM creds + MEMORY_API_KEY eksik — yapılacak iş yok" >> "$LOG"
    echo "OUTCOME: fail | TELEGRAM creds + MEMORY_API_KEY eksik"
    exit 0
fi
[ "$TG_OK" = "0" ] && echo "[$(date -Iseconds)] TELEGRAM creds eksik -> memory-only mod (discovery yazılır, Telegram atlanır)" >> "$LOG"

TG_URL="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

# Aksiyon-önerisi: alert SADECE haber vermesin -> ne-yapmalı + nasıl (tanı-komutu).
# Kaynak-tipine göre (klipper çalışma-akışı: bul→bildir→öner). auto-mode kapalı
# olduğundan öneriler MANUEL; kaynak prefix'inden türetilir.
suggest_action() {
    local src="$1" base name
    base="${src%%:*}"; name="${src#*:}"
    case "$base" in
        memory) echo "🔧 Öneri: \`docker system prune -f\` (volume hariç) + \`pip cache purge\`. ⚠️ Risk: durmuş container/unused-image silinir (çalışanlar etkilenmez). 🔍 Bak: free -h; docker ps --size; ps aux --sort=-%mem | head" ;;
        disk)   echo "🔧 Öneri: docker prune + büyük-log truncate. ⚠️ Risk: unused-image silinir + >50M log'lar 10M'a kırpılır (eski-log-kaybı). 🔍 Bak: df -h; du -sh /var/log/* /opt/linux-ai-server/data/* 2>/dev/null | sort -h | tail" ;;
        cpu)    echo "🔧 Öneri: yük-yapan süreci incele/sınırla. ⚠️ Risk: yok (sadece-inceleme, otomatik-aksiyon yok). 🔍 Bak: ps aux --sort=-%cpu | head; uptime" ;;
        temperature) echo "🔧 Öneri: yükü azalt / governor powersave. ⚠️ Risk: powersave = CPU yavaşlar (performans düşer). 🔍 Bak: sensors; cat /proc/linux_ai" ;;
        service) echo "🔧 Öneri: \`sudo systemctl restart ${name}\`. ⚠️ Risk: ${name} kısa kesinti (restart sırasında). 🔍 Bak: journalctl -u ${name} -n 50 --no-pager" ;;
        docker)  echo "🔧 Öneri: \`docker restart ${name}\`. ⚠️ Risk: kısa kesinti (restart; durmuşsa başlatır, unhealthy'yi düzeltir). 🔍 Bak: docker logs --tail 50 ${name}" ;;
        cron)    echo "🔧 Öneri: log'u incele + işi elle çalıştır. ⚠️ Risk: işe-bağlı (önce log'a bak). 🔍 Bak: tail -40 /var/log/linux-ai-server/${name}.log" ;;
        escalation|remediation) echo "⛔ MANUEL MÜDAHALE GEREK: otonom düzeltme yetmedi/kapalı — '${name}' hâlâ kritik. ⚠️ Çözülene dek pinglenir. Kaynağı elle çöz." ;;
        *) echo "🔧 Öneri: detayı incele + ilgili log'a bak. ⚠️ Risk: bilinmiyor (önce incele). (kaynak: ${src})" ;;
    esac
}

# Hata-hafızası: critical event'i otomatik discovery'e (type=bug) kaydet — "sadece
# hata varsa" (yalnız critical). Stabil başlık (AUTO-alert: <source>) -> server-side
# dedup (5dk + aktif-duplicate-title-update) tekrar eden aynı-hatayı TEK kayıtta tutar
# (spam yok); kaynak çözülüp tekrar bozulursa regression=yeni-active. Best-effort.
# Dönüş: 0 = discovery yazıldı (HTTP 200), 1 = yazılamadı. Memory-only modda notified
# işaretlemesi buna bağlanır (Codex P2: yazım başarısızsa retry, kayıp yok).
save_discovery() {
    local src="$1" title="$2" detail="$3" ts="$4"
    [ -z "$MEMORY_API_KEY" ] && return 1
    local body http
    body=$(TITLE="AUTO-alert: ${src}" DET="${title} | ${detail} (${ts})" python3 -c '
import json, os
print(json.dumps({
    "device_name": "klipper", "project": "linux-ai-server", "type": "bug",
    "title": os.environ["TITLE"][:120], "details": os.environ["DET"][:1000],
    "rationale": "notify-cron otomatik hata-hafızası (critical event).",
}))' 2>/dev/null) || return 1
    http=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 -X POST "${API_BASE}/api/v1/memory/discoveries" \
        -H "Content-Type: application/json" -H "X-Memory-Key: ${MEMORY_API_KEY}" \
        -d "$body" 2>/dev/null)
    [ "$http" = "200" ]
}

# Slice C: kaynağın son RECUR_DAYS gününde kaç critical event'i var (tekrar-deseni).
recur_count() {
    sqlite3 "$DB_PATH" \
        "SELECT COUNT(*) FROM events WHERE source='${1//\'/}' AND severity='critical' AND timestamp > datetime('now','-${RECUR_DAYS} days');" \
        2>/dev/null || echo 0
}

# Slice A: AUTO-alert discovery'lerini kaynak DÜZELDİYSE otomatik resolve et. Uniform
# sinyal: kaynağın son critical event'i > RESOLVE_QUIET_MIN dk önce ise = sessiz = düzeldi
# (sürekli arıza her run critical bastığından sessizlik gerçek-düzelme demek). Saf events
# sorgusu; per-source-tip mantığı yok. Best-effort.
reconcile_autobugs() {
    [ -f "$MEMORY_DB" ] || return 0
    # Codex P2: FIX-PENDING teşhisleri MUAF. Raw auto-alert "kaynak sessiz=düzeldi" ile
    # kapatılabilir; ama auto-investigate'in FIX-PENDING teşhisi alarm geçici sessizleşse
    # bile çözülmemiştir (fix uygulanmadı) — kapatırsak #567'deki gömülme tekrarlanır.
    sqlite3 "$MEMORY_DB" \
        "SELECT id || '|' || substr(title,13) FROM discoveries WHERE type='bug' AND status='active' AND title LIKE 'AUTO-alert: %' AND COALESCE(details,'') NOT LIKE '%FIX-PENDING%';" \
        2>/dev/null | while IFS='|' read -r did dsrc; do
        [ -z "$did" ] || [ -z "$dsrc" ] && continue
        local recent
        recent=$(sqlite3 "$DB_PATH" \
            "SELECT COUNT(*) FROM events WHERE source='${dsrc//\'/}' AND severity='critical' AND timestamp > datetime('now','-${RESOLVE_QUIET_MIN} minutes');" \
            2>/dev/null)
        if [ "${recent:-1}" = "0" ]; then
            sqlite3 "$MEMORY_DB" \
                "UPDATE discoveries SET status='completed', resolved=1, rationale=COALESCE(rationale,'')||' [auto-resolved: kaynak ${RESOLVE_QUIET_MIN}dk sessiz=düzeldi]' WHERE id=${did} AND status='active';" \
                2>>"$LOG" || true
            echo "[$(date -Iseconds)] AUTO-RESOLVE id=${did} src=${dsrc} (${RESOLVE_QUIET_MIN}dk sessiz)" >> "$LOG" 2>/dev/null
        fi
    done
}

# Slice-2: bu kaynağa [🔧 Uygula] tek-tıkla-aksiyon butonu sunulabilir mi?
# devops_agent._executable_playbook ile AYNI küme (memory/disk/temperature/service/
# docker). cpu=sadece-inceleme, cron/diğer=playbook-yok -> sadece [✅ Gördüm].
# escalation:/remediation: önekleri iç-kaynağa indirgenir (orada aksiyon olabilir).
has_action() {
    local s="$1" base
    case "$s" in escalation:*|remediation:*) s="${s#*:}";; esac
    base="${s%%:*}"
    case "$base" in
        memory|disk|temperature|service|docker) return 0 ;;
        *) return 1 ;;
    esac
}

# LIMIT 50: batch/spam-cap (Codex #24). Outage/producer-bug sonrasi sinirsiz burst
# onler; kalan-backlog sonraki */20 run'da drenaj edilir (no-loss korunur).
IDS=$(sqlite3 "$DB_PATH" \
    "SELECT id FROM events WHERE severity IN ('warn','critical') AND notified=0 ORDER BY id ASC LIMIT 50;" \
    2>/dev/null)
q_rc=$?
if [ "$q_rc" -ne 0 ]; then
    echo "[$(date -Iseconds)] sqlite read FAIL rc=$q_rc db=$DB_PATH" >> "$LOG"
    echo "OUTCOME: fail | events okunamadi (sqlite rc=$q_rc)"
    exit 0
fi
# Bekleyen event yok: yine de düzelen AUTO-alert bug'larını kapat (reconcile event-bağımsız).
[ -z "$IDS" ] && { reconcile_autobugs; echo "OUTCOME: pass | no-pending"; exit 0; }

echo "[$(date -Iseconds)] notify-cron: pending events — processing..." >> "$LOG"
sent=0; failed=0

for id in $IDS; do
    [ -z "$id" ] && continue
    row=$(sqlite3 -separator $'\x1f' "$DB_PATH" \
        "SELECT type,source,severity,title,COALESCE(detail,''),timestamp FROM events WHERE id=${id};" \
        2>/dev/null)
    [ -z "$row" ] && continue
    IFS=$'\x1f' read -r type src sev title detail ts <<< "$row"

    SEV_TAG="[WARN]"; [ "$sev" = "critical" ] && SEV_TAG="[CRITICAL]"
    SAFE_TITLE=$(printf '%s' "$title"  | tr -d '<>&"' | tr '\n\r\t' '   ' | head -c 200)
    SAFE_DETAIL=$(printf '%s' "$detail" | tr -d '<>&"' | tr '\n\r\t' '   ' | head -c 300)
    SAFE_SRC=$(printf '%s' "$src" | tr -d '<>&"' | head -c 80)

    # Slice C: tekrar-deseni. critical + kaynağın son RECUR_DAYS'te >=RECUR_THRESHOLD
    # critical'i varsa "🔁 TEKRARLAYAN (Nx)" satırı (aynı sorun tekrar ediyor -> kök-neden).
    RECUR_LINE=""
    if [ "$sev" = "critical" ]; then
        RC=$(recur_count "$src")
        if [ "${RC:-0}" -ge "$RECUR_THRESHOLD" ]; then
            RECUR_LINE="🔁 TEKRARLAYAN (${RC}x / ${RECUR_DAYS}g) — kök-neden incelenmeli
"
            # Slice B (opt-in): tekrarlayan -> arka-planda otonom salt-okunur inceleme.
            # fire-and-forget (notify-cron bloklanmaz); rate-limit+kapı script içinde.
            if [ "${AUTO_INVESTIGATE_ENABLED:-false}" = "true" ] && [ -f "$INVESTIGATE_SCRIPT" ]; then
                AUTO_INVESTIGATE_ENABLED=true python3 "$INVESTIGATE_SCRIPT" "$src" "$RC" >/dev/null 2>&1 &
            fi
        fi
    fi

    MSG="${SEV_TAG} klipper
src: ${SAFE_SRC}
${RECUR_LINE}${SAFE_TITLE}"
    [ -n "$SAFE_DETAIL" ] && MSG="${MSG}
${SAFE_DETAIL}"
    SUGGEST=$(suggest_action "$src")
    MSG="${MSG}

${SUGGEST}
${ts}"

    JSON_MSG=$(printf '%s' "$MSG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
    # inline butonlar — callback'leri telegram-poller process_update yakalar:
    #   [🔧 Uygula] fix:<id>  -> devops force-remediate (playbook çalıştır+verify)
    #   [✅ Gördüm] ack:<id>  -> events.acked=1 -> escalation durur
    # [🔧 Uygula] YALNIZ çalıştırılabilir-playbook olan kaynaklara (has_action).
    if has_action "$src"; then
        BTN_ROW="{\"text\":\"🔧 Uygula\",\"callback_data\":\"fix:${id}\"},{\"text\":\"✅ Gördüm\",\"callback_data\":\"ack:${id}\"}"
    else
        BTN_ROW="{\"text\":\"✅ Gördüm\",\"callback_data\":\"ack:${id}\"}"
    fi
    REPLY_MARKUP="{\"inline_keyboard\":[[${BTN_ROW}]]}"
    BODY="{\"chat_id\":\"${TELEGRAM_CHAT_ID}\",\"text\":${JSON_MSG},\"reply_markup\":${REPLY_MARKUP}}"

    # Hata-hafızası ÖNCE (Telegram'dan BAĞIMSIZ — Codex P2: TG down olsa bile SessionStart
    # critical'ı görsün): yalnız critical -> otomatik discovery. Dedup server-side.
    DISCOVERY_OK=0
    if [ "$sev" = "critical" ]; then
        save_discovery "$SAFE_SRC" "$SAFE_TITLE" "$SAFE_DETAIL" "$ts" && DISCOVERY_OK=1
    fi

    # klipper #100224: kaynak-bazlı Telegram cooldown (flood-bastir). Aynı kaynaktan son
    # cooldown-penceresi içinde mesaj gittiyse bu event'i sil (notified=1 yap) ama
    # Telegram'a GÖNDERME. Root-fix'ler yürürken anlık flood kesici.
    #
    # warn  → NOTIFY_COOLDOWN_SECONDS (default 300=5dk): warn-flapping'i bastırır.
    # critical → CRITICAL_COOLDOWN_SECONDS (default 900=15dk): İLK critical HER ZAMAN
    #   geçer (warn→critical escalation kaçmaz: yalnız 'notified=1 + severity=critical'
    #   geçmişine bakılır, warn-geçmişi tetiklemez). Sadece aynı-kaynağın YAKIN-ZAMANDA
    #   ZATEN-BİLDİRİLMİŞ critical'i varsa SONRAKİLER collapse olur. Hedef: restart-storm
    #   (in-memory _active_alerts sıfırlanıp _detect aynı critical'i yeniden emit eder)
    #   + flapping (resolve→re-alert) + outage-burst. save_discovery zaten cooldown-ÖNCESİ
    #   çalıştığı için collapse'ta bile hata-hafızası/auto-bug yaşar — yalnız Telegram susar.
    #   escalation:/remediation: önekleri MUAF — devops _escalate_persistent'te zaten
    #   30dk'da-bir throttle'lı kasıtlı hatırlatmalar (çift-throttle yok).
    NOTIFY_COOLDOWN_SECONDS="${NOTIFY_COOLDOWN_SECONDS:-300}"
    CRITICAL_COOLDOWN_SECONDS="${CRITICAL_COOLDOWN_SECONDS:-900}"
    COOLDOWN_OK=1
    if [ "$TG_OK" = "1" ] && [ "$sev" != "critical" ] && [ "${NOTIFY_COOLDOWN_SECONDS}" -gt 0 ]; then
        last_notified_age=$(sqlite3 "$DB_PATH" \
            "SELECT CAST((julianday('now') - julianday(MAX(timestamp))) * 86400 AS INTEGER)
             FROM events WHERE source='${src//\'/}' AND notified=1;" 2>/dev/null)
        if [ -n "$last_notified_age" ] && [ "$last_notified_age" -lt "$NOTIFY_COOLDOWN_SECONDS" ] 2>/dev/null; then
            sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
            echo "[$(date -Iseconds)] COOLDOWN id=${id} src=${SAFE_SRC} last=${last_notified_age}s<${NOTIFY_COOLDOWN_SECONDS}s" >> "$LOG"
            COOLDOWN_OK=0
        fi
    elif [ "$TG_OK" = "1" ] && [ "$sev" = "critical" ] && [ "${CRITICAL_COOLDOWN_SECONDS}" -gt 0 ]; then
        case "$src" in
            escalation:*|remediation:*) : ;;  # MUAF: devops 30dk-throttle'lı kasıtlı hatırlatma
            *)
                last_crit_age=$(sqlite3 "$DB_PATH" \
                    "SELECT CAST((julianday('now') - julianday(MAX(timestamp))) * 86400 AS INTEGER)
                     FROM events WHERE source='${src//\'/}' AND severity='critical' AND notified=1;" 2>/dev/null)
                if [ -n "$last_crit_age" ] && [ "$last_crit_age" -lt "$CRITICAL_COOLDOWN_SECONDS" ] 2>/dev/null; then
                    sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
                    echo "[$(date -Iseconds)] COOLDOWN-CRIT id=${id} src=${SAFE_SRC} last=${last_crit_age}s<${CRITICAL_COOLDOWN_SECONDS}s (collapse; ilk-critical zaten bildirildi)" >> "$LOG"
                    COOLDOWN_OK=0
                fi
                ;;
        esac
    fi

    # Telegram gönderimi (yalnız TG_OK ve cooldown geçmişse). Memory-only modda atlanır.
    if [ "$TG_OK" = "1" ] && [ "$COOLDOWN_OK" = "1" ]; then
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
            -X POST "$TG_URL" \
            -H "Content-Type: application/json" \
            -d "$BODY" 2>/dev/null)
    elif [ "$COOLDOWN_OK" = "0" ]; then
        HTTP="cooldown"
    else
        HTTP="no-tg"
    fi

    if [ "$HTTP" = "200" ] || [ "$HTTP" = "cooldown" ]; then
        [ "$HTTP" = "200" ] && sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
        [ "$HTTP" = "200" ] && echo "[$(date -Iseconds)] SENT id=${id} src=${SAFE_SRC} sev=${sev}" >> "$LOG"
        sent=$((sent + 1))
    elif [ "$TG_OK" = "0" ]; then
        # Memory-only: critical hafızaya YAZILDIYSA handled (notified=1). Yazılamadıysa
        # (Codex P2) notified=0 kalır -> sonraki run retry (kayıp yok). warn -> ertele.
        if [ "$sev" = "critical" ] && [ "$DISCOVERY_OK" = "1" ]; then
            sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
            echo "[$(date -Iseconds)] MEMORY-ONLY id=${id} src=${SAFE_SRC} critical->hafıza (Telegram yok)" >> "$LOG"
            sent=$((sent + 1))
        else
            # Ertelendi (notified=0, retry bekliyor) -> failed say (Codex P2): yoksa OUTCOME
            # 'pass | sent=0' der ve sağlık-izleme bekleyen-bildirimi göremez (creds-eksik
            # fail-sinyali kaybolur). sent=0+failed>0 -> OUTCOME:fail; karışıksa partial.
            echo "[$(date -Iseconds)] DEFER id=${id} src=${SAFE_SRC} sev=${sev} discovery_ok=${DISCOVERY_OK} (retry sonraki run)" >> "$LOG"
            failed=$((failed + 1))
        fi
    else
        echo "[$(date -Iseconds)] FAIL id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP} — retry next run" >> "$LOG"
        failed=$((failed + 1))
    fi
    sleep 1
done

# Slice A: kaynağı düzelen AUTO-alert bug'larını otomatik kapat (lifecycle temizliği).
reconcile_autobugs

echo "[$(date -Iseconds)] notify-cron done: sent=${sent} failed=${failed}" >> "$LOG"

# OUTCOME-contract (FAZ1; Codex #23/#24): notify-cron'un kendi saglik sinyali.
# fail: bildirim-pipeline down (Telegram unreachable); partial: bazi iletildi.
if [ "${failed:-0}" -gt 0 ] && [ "${sent:-0}" -eq 0 ]; then
    echo "OUTCOME: fail | notify-pipeline down: sent=0 failed=${failed} (pending-retry, NO-LOSS)"
elif [ "${failed:-0}" -gt 0 ]; then
    echo "OUTCOME: partial | sent=${sent} failed=${failed} (pending-retry)"
else
    echo "OUTCOME: pass | sent=${sent}"
fi
