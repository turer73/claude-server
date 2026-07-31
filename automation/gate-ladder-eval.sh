#!/bin/bash
# gate-ladder-eval.sh — G6 haftalık değerlendirme cron'u (recommend-only).
# Tasarım: docs/g6-enforcement-ladder-design.md §4. RECOMMEND-ONLY: branch-protection'a
# DOKUNMAZ; öneri üretir + Turgut'a not-atar. Aktüasyon insan-tetikli helper'larla (§4).
#
# CRON-KURULUM (klipper-deploy, haftalık — telemetri-ritmi):
#   0 6 * * 1 /opt/linux-ai-server/automation/gate-ladder-eval.sh
#
# Env: COVERAGE_DB, REPORT_DAYS, HOOK_ENV_FILE (note-key), GATE_LADDER_LOG.

set -uo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"
DAYS="${REPORT_DAYS:-30}"
LOG_FILE="${GATE_LADDER_LOG:-/opt/linux-ai-server/data/hook-logs/gate-ladder.log}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

case "$DAYS" in
    ''|*[!0-9]*) echo "HATA: REPORT_DAYS sayısal olmalı (verilen: $DAYS)" >&2; exit 1 ;;
esac

# Tablolar yoksa bootstrap (idempotent) — eval tek-başına ayağa kalkabilir.
# gate_telemetry (G2) DE gerekli: production_stats onu okur; fresh-DB'de yoksa 'no such table'.
COVERAGE_DB="$DB" bash "$SELF_DIR/../scripts/migrate-gate-telemetry.sh" >/dev/null 2>>"$LOG_FILE" || {
    echo "OUTCOME: fail | gate_telemetry migration FAIL"; log "gate_telemetry migration FAIL"; exit 2; }
COVERAGE_DB="$DB" bash "$SELF_DIR/../scripts/migrate-gate-ladder.sh" >/dev/null 2>>"$LOG_FILE" || {
    echo "OUTCOME: fail | gate_ladder migration FAIL"; log "gate_ladder migration FAIL"; exit 2; }

# Öneri-raporunu üret (python-çekirdek gate_ladder'ı da günceller: last_eval + history).
# PYTHONIOENCODING: rapor emoji+Türkçe içerir; CI-locale ASCII ise print UnicodeEncodeError verir.
REPORT=$(COVERAGE_DB="$DB" REPORT_DAYS="$DAYS" PYTHONIOENCODING=utf-8 python3 -c "
import os, sqlite3, sys
sys.path.insert(0, '$SELF_DIR')
from gate_ladder_eval import run_eval, format_report, production_stats
conn = sqlite3.connect(os.environ['COVERAGE_DB'])
days = int(os.environ['REPORT_DAYS'])
recs = run_eval(conn, days)
stats = production_stats(conn, days)
unc = {g: s.unclassified for g, s in stats.items()}
print(format_report(recs, unc))
conn.close()
" 2>>"$LOG_FILE") || { echo "OUTCOME: fail | eval çekirdeği hata verdi"; log "eval FAIL"; exit 2; }

printf '%s\n' "$REPORT" | tee -a "$LOG_FILE"

# Aktüasyon-önerisi VARSA Turgut'a not (fail-safe: KEY yoksa sessiz-skip, rapor log'da kalır).
if printf '%s' "$REPORT" | grep -qE 'TERFİ-ÖNERİSİ|DÜŞÜR-ÖNERİSİ'; then
    REPORT="$REPORT" HOOK_ENV_FILE="${HOOK_ENV_FILE:-/opt/linux-ai-server/.env}" python3 <<'PY' 2>>"$LOG_FILE" || true
import json, os, sys, urllib.request
_env = os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')
try:
    _keys = [l.split('=', 1)[1].strip() for l in open(_env).read().splitlines() if l.startswith('MEMORY_API_KEY=')]
except OSError:
    print(f'env-dosyası yok ({_env}) — G6-note atlandı', file=sys.stderr); raise SystemExit(0)
if not _keys:
    print('MEMORY_API_KEY yok — G6-note atlandı', file=sys.stderr); raise SystemExit(0)
body = json.dumps({
    'from_device': 'klipper',
    'title': 'G6 enforcement-ladder: aktüasyon-önerisi (Turgut-onayı bekliyor)',
    'content': os.environ['REPORT'],
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/notes', data=body, method='POST',
    headers={'Content-Type': 'application/json; charset=utf-8', 'X-Memory-Key': _keys[0]})
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception as e:
    print(f'G6-note POST hatası: {e}', file=sys.stderr)
PY
    log "aktüasyon-önerisi Turgut'a bildirildi"
fi

log "eval bitti"

# OUTCOME marker (cron-wrap sözleşmesi, tools/lint-cron-outcome.sh zorunlu kılar): öneri-sayısı
# raporun son satırındaki "# N aktüasyon-önerisi"nden okunur. 0 öneri = sağlıklı hold durumu.
RECS=$(printf '%s' "$REPORT" | grep -oE '^# [0-9]+ aktüasyon-önerisi' | grep -oE '[0-9]+' | tail -1)
echo "OUTCOME: pass | gate-ladder-eval: ${RECS:-0} aktüasyon-önerisi (recommend-only)"
exit 0
