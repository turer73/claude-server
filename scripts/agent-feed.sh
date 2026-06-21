#!/usr/bin/env bash
# agent-feed.sh — ORTAK AJAN-SİNYAL FEED'i (klipper'a tek-bakış özet).
#
# NEDEN: ajan sinyalleri 6+ ayrı yere dağılmıştı (notes / alerts / events-code-review /
# discoveries / cron_outcomes / Haiku-heartbeat) ve hiçbiri bütünleşik klipper'a ulaşmıyordu
# (kullanıcı: "ortak sistem kur tüm ajanlardan gelen bilgileri toplayıp sana bilgi verecek").
# Bu script o kaynakları TEK feed'de birleştirir; SessionStart hook çağırır + elle çalıştırılır.
#
# Salt-okunur (Codex-cache hariç hiçbir yan-etki yok). LOCAL-only (ağ yok → session-start hızlı).
# FAIL-SAFE: her sorgu 2>/dev/null, eksik DB/dosya → o satır atlanır, ASLA hata vermez.
#
# Kullanım: bash scripts/agent-feed.sh [--device klipper] [--hours 24]
set -uo pipefail

MEM_DB="${AGENT_FEED_MEM_DB:-/opt/linux-ai-server/data/claude_memory.db}"
SRV_DB="${AGENT_FEED_SRV_DB:-/opt/linux-ai-server/data/server.db}"
HB="${AGENT_FEED_HB:-/opt/linux-ai-server/data/hook-state/last-code-review.json}"
CODEX_CACHE="${AGENT_FEED_CODEX:-/opt/linux-ai-server/data/hook-state/codex-open.txt}"
DEV="klipper"
HOURS=24
while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEV="$2"; shift 2 ;;
        --hours) HOURS="$2"; shift 2 ;;
        *) shift ;;
    esac
done

q() { sqlite3 "$1" "$2" 2>/dev/null; }
LINES=()
add() { LINES+=("$1"); }

# ── 🔬 Haiku kod-review: son verdict (heartbeat) + son commit-bulguları ──
if [ -r "$HB" ]; then
    HB_LINE=$(python3 - "$HB" <<'PY' 2>/dev/null
import json, sys, datetime
try:
    d = json.load(open(sys.argv[1]))
    ts = d.get("ts", "")[:16].replace("T", " ")
    v = "TEMİZ" if d.get("clean") else f"{d.get('findings', '?')} bulgu"
    print(f"🔬 Haiku son review: {v} ({d.get('files', '?')} dosya, {d.get('trigger', '?')}) {ts}")
except Exception:
    pass
PY
)
    [ -n "${HB_LINE:-}" ] && add "$HB_LINE"
fi
# GERÇEK commit-bulguları (eski qwen '(sweep)' FP-selini DIŞLA — yalnız '(commit)' Haiku)
CR_FINDINGS=$(q "$SRV_DB" "SELECT '   └ ' || substr(replace(title,'🔬 Kod-review ',''),1,60) FROM events WHERE source LIKE 'code-review:%' AND title LIKE '%(commit)%' AND timestamp > datetime('now','-${HOURS} hours') ORDER BY timestamp DESC LIMIT 4")
[ -n "$CR_FINDINGS" ] && add "🔬 Haiku bulgu (DOĞRULA):" && add "$CR_FINDINGS"

# ── 🤖 Codex: açık PR bulguları (cache dosyasından — codex-feed-poll cron yazar) ──
if [ -r "$CODEX_CACHE" ]; then
    CODEX=$(grep -v '^#' "$CODEX_CACHE" 2>/dev/null | head -4)
    [ -n "$CODEX" ] && add "$CODEX"
fi

# ── 📝 Notlar: okunmamış (per-device) ──
HAS_RB=$(q "$MEM_DB" "SELECT COUNT(*) FROM pragma_table_info('notes') WHERE name='read_by'")
if [ "${HAS_RB:-0}" -gt 0 ]; then
    PRED="read=0 AND (read_by IS NULL OR read_by NOT LIKE '%|$DEV|%')"
else
    PRED="read=0"
fi
NCNT=$(q "$MEM_DB" "SELECT COUNT(*) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $PRED")
if [ "${NCNT:-0}" -gt 0 ]; then
    NTOP=$(q "$MEM_DB" "SELECT from_device || ': ' || substr(title,1,48) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $PRED ORDER BY created_at DESC LIMIT 1")
    add "📝 Not: $NCNT okunmamış — $NTOP"
fi

# ── 🌡️ Sistem alarmları: çözülmemiş, son 6h ──
ALCNT=$(q "$SRV_DB" "SELECT COUNT(DISTINCT source||message) FROM alerts WHERE resolved=0 AND timestamp > datetime('now','-6 hours')")
if [ "${ALCNT:-0}" -gt 0 ]; then
    ALTOP=$(q "$SRV_DB" "SELECT '[' || severity || '] ' || source || ': ' || substr(message,1,40) FROM alerts WHERE resolved=0 AND timestamp > datetime('now','-6 hours') ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, timestamp DESC LIMIT 1")
    add "🌡️ Alarm: $ALCNT açık — $ALTOP"
fi

# ── 🩺 Cron: tekrarlayan critical (son 7g >=3) ──
RECUR=$(q "$MEM_DB" "ATTACH '${SRV_DB}' AS srv; SELECT substr(d.title,13,40) || ' (🔁' || (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) || 'x)' FROM discoveries d WHERE d.type='bug' AND d.status='active' AND d.title LIKE 'AUTO-alert: %' AND (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) >= 3 ORDER BY d.created_at DESC LIMIT 2")
[ -n "$RECUR" ] && add "🩺 Cron tekrar-hata: $(echo "$RECUR" | tr '\n' '|' | sed 's/|$//')"

# ── Çıktı ──
if [ ${#LINES[@]} -eq 0 ]; then
    echo "🛰️ AJAN FEED (son ${HOURS}h): sinyal yok — tüm ajanlar sessiz/temiz."
else
    echo "🛰️ AJAN FEED (son ${HOURS}h — tüm ajanlar):"
    for l in "${LINES[@]}"; do echo "$l"; done
fi
exit 0
