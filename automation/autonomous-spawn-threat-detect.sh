#!/bin/bash
# autonomous-spawn-threat-detect.sh — Spawn log threat indicator scanner (P1.6)
#
# Repo 2 (AI-Autonomous-Core) inspired pasif tehdit tespit:
# autonomous spawn output'unda credential read, exfil, persistence,
# lateral movement, anti-forensic, reverse shell pattern'leri arar.
# Tespit -> memory entry + Telegram alert. AUTO-BLOCK YOK (false positive
# spawn'i bosa cevirir; manuel review).
#
# Komplementer:
# - P0.5 autonomous-spawn-audit.sh: git diff inceler (suspicious commit)
# - P1.6 (bu): spawn_log inceler (suspicious komut/cikti)
#
# Kullanim: autonomous-spawn-threat-detect.sh <NOTE_ID> <SPAWN_LOG>

set -uo pipefail

NOTE_ID="${1:-}"
SPAWN_LOG="${2:-}"
LOG_FILE="${THREAT_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"

if [ -z "$NOTE_ID" ] || [ -z "$SPAWN_LOG" ]; then
    echo "Usage: $0 <NOTE_ID> <SPAWN_LOG>" >&2
    exit 2
fi

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] threat-detect: %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

if [ ! -f "$SPAWN_LOG" ]; then
    log "spawn_log missing #$NOTE_ID: $SPAWN_LOG"
    exit 0
fi

declare -a HITS=()

scan() {
    local label="$1" pattern="$2"
    local match
    match=$(grep -aoE "$pattern" "$SPAWN_LOG" 2>/dev/null | head -1)
    if [ -n "$match" ]; then
        local short
        short=$(printf '%s' "$match" | tr -d '\n' | head -c 200)
        HITS+=("$label: $short")
    fi
}

# ───── Credential read ─────
scan "cred-env"     'cat[[:space:]]+[^[:space:]|]*\.env([[:space:]]|$)'
scan "cred-ssh"     'cat[[:space:]]+[^[:space:]|]*\.ssh/(id_[a-z0-9_]+|authorized_keys)([[:space:]]|$)'
scan "cred-aws"     'cat[[:space:]]+[^[:space:]|]*\.aws/credentials'
scan "cred-shadow"  'cat[[:space:]]+(/etc/shadow|/etc/sudoers)([[:space:]]|$)'
scan "cred-k8s"     'kubectl[[:space:]]+get[[:space:]]+secret[^|]*-o[[:space:]]+(yaml|json)'

# ───── Exfiltration ─────
# (eski scan_remote() bozuktu: local label= pattern= / match= / if [ -n ] → arg'ları hiç
#  kullanmıyordu, exfil-curl-pipe FİİLEN HİÇ TARANMIYORDU. Normal scan'e çevrildi.)
scan "exfil-curl-pipe"  '(curl|wget)[[:space:]][^|;]*\|[[:space:]]*(bash|sh|python|python3)'
scan "exfil-base64-net" 'base64[[:space:]][^|;]*\|[[:space:]]*(curl|wget|nc|ncat)'
scan "exfil-curl-file"  'curl[[:space:]][^;|]*-(F|-data-binary)[[:space:]]@'

# ───── Persistence ─────
scan "persist-cron"     'crontab[[:space:]]+-e([[:space:]]|$)|>>?[[:space:]]*/etc/cron'
scan "persist-bashrc"   '>>?[[:space:]]*[^[:space:]|]*\.(bashrc|zshrc|bash_profile|profile)([[:space:]]|$)'
scan "persist-systemd"  'systemctl[[:space:]]+enable[[:space:]]'
scan "persist-authkey"  '>>?[[:space:]]*[^[:space:]|]*\.ssh/authorized_keys'

# ───── Lateral movement ─────
scan "lateral-ssh"      'ssh[[:space:]]+[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9.\-]+'
scan "lateral-scp"      'scp[[:space:]][^|;]*[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9.\-]+:'
scan "lateral-rsync"    'rsync[[:space:]][^|;]*[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9.\-]+:'

# ───── Anti-forensic ─────
scan "antifor-history"  '(history[[:space:]]+-c|unset[[:space:]]+HISTFILE|export[[:space:]]+HISTFILE=/dev/null)'
scan "antifor-logs"     'journalctl[[:space:]]+--(rotate|vacuum)|>[[:space:]]*/var/log/'
scan "antifor-shred"    'shred[[:space:]]+[^[:space:]|]+|wipe[[:space:]]+[^[:space:]|]+'

# ───── Reverse shell ─────
scan "rshell-nc"        '(nc|ncat)[[:space:]]+(-l[[:space:]]|-e[[:space:]]|-c[[:space:]])'
scan "rshell-bash-tcp"  'bash[[:space:]]+-i[[:space:]]+>&[[:space:]]*/dev/tcp/'
scan "rshell-python"    'python3?[[:space:]]+-c[[:space:]]+[^[:space:]]*socket\.socket'

# ───── Drop & exec ─────
scan "drop-tmp-exec"    '(wget|curl)[[:space:]][^|;]*-O[[:space:]]+/tmp/[^[:space:]]+[[:space:]]*(&&|;)[[:space:]]*(bash|sh|chmod)'

# ───── Sonuc ─────
if [ "${#HITS[@]}" -eq 0 ]; then
    log "scan clean #$NOTE_ID"
    exit 0
fi

# FP-AZALT (cred-fix sonrası spawn'lar başarıyla çalışınca threat-detect benign
# result-PROSE'unu komut-regex'lerine takıp her başarılı spawn'ı "URGENT" sayıyordu).
# Spawn log = `claude -p --output-format json` = result-PROSE (komut-transkripti DEĞİL);
# low-confidence pattern'ler (ssh user@host, systemctl enable, journalctl, crontab…)
# claude'un düz-metin açıklamasında/enjekte-context'te kolayca eşleşir → FP.
# Strateji: HIGH-confidence hit (reverse-shell/exfil-pipe/drop-exec/cred-dosya) benign
# prose'da neredeyse çıkmaz → her zaman flag. YALNIZ low-confidence + spawn-benign-başarılı
# (is_error=false) → FP, FLAG'LEME. Spawn hatalıysa (is_error!=false) low de korunur.
IS_ERROR=$(grep -aoE '"is_error":[[:space:]]*(true|false)' "$SPAWN_LOG" 2>/dev/null | head -1 | grep -oE 'true|false')
# cred-* HEPSI HIGH (Codex P1): pattern'ler gerçek KOMUT gerektirir ('cat .env' / 'cat .ssh/id_*'
# — bare '.env' prose DEĞİL), bu yüzden benign-success'ta bile cred-read sinyali korunmalı.
HIGH_RE='^(rshell-|exfil-|drop-tmp-exec|cred-|persist-authkey)'
has_high=0
for h in "${HITS[@]}"; do
    [[ "${h%%:*}" =~ $HIGH_RE ]] && has_high=1
done
if [ "$has_high" -eq 0 ] && [ "$IS_ERROR" = "false" ]; then
    log "scan: benign spawn #$NOTE_ID (is_error=false) yalniz low-confidence hit (${#HITS[@]}) -> FP, atlandi: ${HITS[*]}"
    exit 0
fi

log "THREAT INDICATORS #$NOTE_ID: ${#HITS[@]} hit(s) (high=$has_high is_error=$IS_ERROR)"

HITS_LIST=$(printf -- '- %s\n' "${HITS[@]}")
NOTE_ID_VAR="$NOTE_ID" HITS_VAR="$HITS_LIST" \
DATE_VAR="$(date -u +%Y%m%d-%H%M)" SPAWN_LOG_VAR="$SPAWN_LOG" \
HIT_COUNT_VAR="${#HITS[@]}" \
python3 <<'PY' 2>>"$LOG_FILE" || true
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-threat-detect-{os.environ['NOTE_ID_VAR']}-{os.environ['DATE_VAR']}",
    'description': f"Threat indicator(s) [{os.environ['HIT_COUNT_VAR']}] — note #{os.environ['NOTE_ID_VAR']} autonomous spawn",
    'content': f"## Autonomous spawn threat detection (P1.6) — manuel inceleme\n\n**Note:** #{os.environ['NOTE_ID_VAR']}\n**Spawn log:** {os.environ['SPAWN_LOG_VAR']}\n**Hit count:** {os.environ['HIT_COUNT_VAR']}\n\n## Indicators\n{os.environ['HITS_VAR']}\n\n## Aksiyon\n```bash\n# Detayli incele:\ncat {os.environ['SPAWN_LOG_VAR']}\n\n# False positive ise (Claude'un dokumante ettigi pattern):\n# bu memory entry'i archive et\n\n# Gerçek tehdit ise:\n# 1. autonomous-claude-settings.json deny list'i guncelle\n# 2. Iliskili commit'leri incele/revert (P0.5 audit dahil)\n# 3. spawn_failures DLQ kontrol et\n# 4. Token rotation: dashboard /admin/secrets'tan etkilenen secret'lari guncelle\n```\n\n**ONEMLI:** Pattern match high-recall, false positive olabilir. Manuel review zorunlu. Auto-block YAPILMADI.",
    'source_device': 'klipper-autonomous',
    'rationale': 'P1.6 threat detect (Repo 2 inspired) — manuel review required, no auto-block'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'memory write err: {e}')
PY

HITS_ESC=$(printf '%s\n' "${HITS[@]}" | python3 -c 'import sys,html; sys.stdout.write(html.escape(sys.stdin.read()[:1500]))' 2>/dev/null || printf '%s\n' "${HITS[@]}")
TG_MSG="<b>🛡 Autonomous Spawn — Threat Indicators</b>

<b>Note:</b> #${NOTE_ID}
<b>Hit count:</b> ${#HITS[@]}

<b>Indicators:</b>
<pre>${HITS_ESC}</pre>

<i>Incele:</i> <code>cat ${SPAWN_LOG}</code>
<i>High-recall scan — false positive olabilir. Manuel review zorunlu.</i>"

# Telegram yerine klipper->surer URGENT not (oturumlar notes API ile haberlesir)
MK=$(grep '^MEMORY_API_KEY=' /opt/linux-ai-server/.env 2>/dev/null | cut -d= -f2-)
# BUG-FIX: eskiden not'a ham SPAWN_LOG'un ilk 800c'i (result-JSON) basılıyordu — yanıltıcı.
# Artık GERÇEK eşleşen indicator'ları (label: match) + log-yolunu basıyoruz.
NOTE_BODY=$(HITS_VAR="$HITS_LIST" NOTE_ID_VAR="$NOTE_ID" SPAWN_LOG_VAR="$SPAWN_LOG" \
    HIGH_VAR="$has_high" ISERR_VAR="${IS_ERROR:-?}" python3 -c "
import json, os
print(json.dumps({'from_device':'klipper',
    'title':'URGENT: Threat #'+os.environ['NOTE_ID_VAR']+' — autonomous spawn',
    'content': 'Threat-detect indicators (high='+os.environ['HIGH_VAR']+' is_error='+os.environ['ISERR_VAR']+'):\n'
        + os.environ['HITS_VAR'][:800]
        + '\n\nIncele: cat '+os.environ['SPAWN_LOG_VAR']
        + '\n(High-recall — false positive olabilir, manuel review.)'}, ensure_ascii=False))
" 2>/dev/null)
curl -sf -X POST http://127.0.0.1:8420/api/v1/memory/notes \
    -H "X-Memory-Key: $MK" -H "Content-Type: application/json" \
    -d "$NOTE_BODY" >> "$LOG_FILE" 2>&1 \
    && log "threat note sent to surer: #$NOTE_ID" \
    || log "threat note send failed for #$NOTE_ID"

exit 0
