#!/bin/bash
# autonomous-daily-summary.sh — gunluk aktivite ozeti
#
# Cron: her gun 06:00 calisir. Onceki 24 saatin autonomous mode aktivitesini
# ozetleyen memory entry yazar. Kullanici sabah session acinca SessionStart
# hook ile MEMORY.md uzerinden gorur.
#
# Topladigi metrikler:
#   - Spawn count + cost (Claude tier)
#   - Classification distribution (ACK/ACTIONABLE/DISCUSSION/URGENT)
#   - Confidence distribution (HIGH/MEDIUM/LOW)
#   - Defer rate (mark read YAPILMAYAN not sayisi)
#   - Defer reasons (low confidence vs discussion vs urgent)
#   - Notes processed by source device
#   - Anomalies: URGENT spike, repeated DEFER, etc.

set -euo pipefail

DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"
LOG_DIR="${HOOK_LOG_DIR:-/opt/linux-ai-server/data/hook-logs}"
SUMMARY_LOG="$LOG_DIR/autonomous-daily-summary.log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$SUMMARY_LOG"; }

mkdir -p "$LOG_DIR"

# Onceki 24 saat
SINCE=$(date -u -d "24 hours ago" '+%Y-%m-%d %H:%M:%S')
NOW=$(date -u '+%Y-%m-%d %H:%M:%S')

# Autonomous log analizi (son 24 saat)
TODAY_LOG=$(awk -v since="$SINCE" '
    BEGIN { FS=" " }
    /^\[/ {
        ts = $1 " " $2
        gsub(/[\[\]]/, "", ts)
        if (ts >= since) print
    }
' "$LOG_DIR/autonomous-claude.log" 2>/dev/null)

# Why `|| true`: grep -c no-match'te 0 yazip exit=1 doner.
# Eski `... | tr -d... || echo 0` patterni hem grep'in 0'ini hem echo'nun 0'ini
# basinca SPAWN_COUNT="00" gibi cosmetic bug uretiyordu. grep -c zaten her zaman
# valid bir sayisal cikti veriyor; sadece exit-code'u true ile maskeliyoruz.
SPAWN_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "spawn complete" || true)
CLASSIFIED_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "classified as:" || true)
ACK_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "ACK route" || true)
ACTIONABLE_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "ACTIONABLE route" || true)
DISCUSSION_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "DISCUSSION route" || true)
URGENT_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "URGENT route" || true)
SKIP_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "^skip\|^throttled" 2>/dev/null | head -1 | tr -d ' \n' || echo 0)
LOW_CONF_COUNT=$(printf '%s\n' "$TODAY_LOG" | grep -c "LOW confidence" 2>/dev/null | head -1 | tr -d ' \n' || echo 0)

# Cost (Claude tier — sadece spawn json log'larindan oku)
TOTAL_COST=$(find "$LOG_DIR" -name "autonomous-claude-spawn-*.log" -newermt "$SINCE" 2>/dev/null | xargs -I{} python3 -c "
import json, sys
try:
    for line in open('{}'):
        line=line.strip()
        if line.startswith('{'):
            d=json.loads(line)
            print(d.get('total_cost_usd', 0))
            break
except: pass
" 2>/dev/null | python3 -c "
import sys
total=0.0
for line in sys.stdin:
    try: total += float(line.strip())
    except: pass
print(f'{total:.4f}')
")

# Spawn detay (cost+turn breakdown)
SPAWN_DETAIL=$(find "$LOG_DIR" -name "autonomous-claude-spawn-*.log" -newermt "$SINCE" 2>/dev/null | sort | xargs -I{} python3 -c "
import json, os
fname=os.path.basename('{}')
try:
    for line in open('{}'):
        line=line.strip()
        if line.startswith('{'):
            d=json.loads(line)
            print(f\"  - {fname}: turns={d.get('num_turns','?')}, cost=\${d.get('total_cost_usd',0):.4f}\")
            break
except: pass
")

# DB'den son 24 saat note + memory metrikleri
NOTES_IN=$(sqlite3 "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='klipper' OR to_device IS NULL) AND created_at > datetime('now', '-24 hours');" 2>/dev/null | head -1 | tr -d ' \n' || echo 0)
MEMORY_NEW=$(sqlite3 "$DB" "SELECT COUNT(*) FROM memories WHERE created_at > datetime('now', '-24 hours');" 2>/dev/null | head -1 | tr -d ' \n' || echo 0)
DEFERRED_NOTES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='klipper' OR to_device IS NULL) AND read=0 AND created_at > datetime('now', '-24 hours');" 2>/dev/null | head -1 | tr -d ' \n' || echo 0)

# Memory entry olustur
DATE=$(date '+%Y-%m-%d')
SLUG="autonomous-daily-summary-$DATE"

SUMMARY_CONTENT=$(cat <<EOF
# Otonom Mod Gunluk Aktivite Ozeti — $DATE

Onceki 24 saatin klipper-side autonomous activity ozeti.

## Toplam Metrikler

| Metrik | Deger |
|---|---|
| Yeni not (klipper-yonelik veya broadcast) | $NOTES_IN |
| Classify edilen not | $CLASSIFIED_COUNT |
| Claude spawn (ACTIONABLE) | $SPAWN_COUNT |
| Toplam etkin cost (Max plan kapsami) | \$$TOTAL_COST |
| Skip/throttle | $SKIP_COUNT |
| LOW confidence defer | $LOW_CONF_COUNT |
| Yeni memory entry | $MEMORY_NEW |
| Hala unread (deferred) | $DEFERRED_NOTES |

## Route Dagilimi

- **ACK** (local handle): $ACK_COUNT
- **ACTIONABLE** (Claude spawn): $ACTIONABLE_COUNT
- **DISCUSSION** (defer): $DISCUSSION_COUNT
- **URGENT** (alert + defer): $URGENT_COUNT

## Spawn Detayi (cost + turn)

$SPAWN_DETAIL

## Anomali Kontrol

EOF
)

# Anomali tespit
if [ "$URGENT_COUNT" -gt 3 ]; then
    SUMMARY_CONTENT="$SUMMARY_CONTENT

⚠️  **URGENT spike: $URGENT_COUNT** — 24 saat icinde 3'ten fazla URGENT. Kullanici hemen bakmali."
fi

if [ "$DEFERRED_NOTES" -gt 5 ]; then
    SUMMARY_CONTENT="$SUMMARY_CONTENT

⚠️  **Yuksek defer: $DEFERRED_NOTES** — kullanici karari bekleyen not sayisi yuksek. Inbox temizligi gerek."
fi

if [ "$SPAWN_COUNT" -gt 50 ]; then
    SUMMARY_CONTENT="$SUMMARY_CONTENT

⚠️  **Yuksek spawn: $SPAWN_COUNT** — Max plan quota dikkat. Throttle yetersiz olabilir."
fi

# Memory'e yaz
RESP=$(SUMMARY="$SUMMARY_CONTENT" SLUG="$SLUG" DATE="$DATE" python3 <<'PY'
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': os.environ['SLUG'],
    'description': f"Otonom mod gunluk aktivite ozeti {os.environ['DATE']} (cron)",
    'content': os.environ['SUMMARY'],
    'source_device': 'klipper-autonomous',
    'rationale': 'Daily summary cron — autonomous mode observability'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try:
    print(urllib.request.urlopen(req, timeout=5).read().decode())
except Exception as e:
    print(f'write error: {e}')
PY
)

log "summary written: $RESP"
log "stats: notes_in=$NOTES_IN spawns=$SPAWN_COUNT cost=\$$TOTAL_COST deferred=$DEFERRED_NOTES"
