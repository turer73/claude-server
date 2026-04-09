#!/bin/bash
# E2E Live Site Tests — Daily automated run
# Cron: 0 7 * * * (her gün 07:00)
# Renderhane, PetVet, Kuafor, Panola canlı kullanıcı testleri.
source /opt/linux-ai-server/.env 2>/dev/null

LOG=/var/log/linux-ai-server/e2e-live.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DIR=/opt/linux-ai-server/e2e-live

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Get Renderhane API key from VPS
RH_KEY=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 ${VPS_HOST:?Set VPS_HOST} \
    "grep RENDERHANE_API_KEY /opt/panola-social/.env | cut -d= -f2" 2>/dev/null)

cd "$DIR" || exit 1

# Run tests (skip API v1 to save credits — run manually when needed)
RENDERHANE_API_KEY="$RH_KEY" \
E2E_EMAIL="${E2E_EMAIL:-demo@panola.app}" \
E2E_PASSWORD="${E2E_PASSWORD:?Set E2E_PASSWORD in .env}" \
npx playwright test --grep-invert="API v1" --reporter=json 2>/dev/null > results.json

# Parse results
TOTAL=$(python3 -c "
import json
with open('results.json') as f:
    data = json.load(f)
suites = data.get('suites', [])
passed = failed = skipped = 0
failures = []
for suite in suites:
    for spec in suite.get('specs', []):
        for test in spec.get('tests', []):
            status = test.get('status', '')
            if status == 'expected':
                passed += 1
            elif status == 'unexpected':
                failed += 1
                title = spec.get('title', '?')
                project = test.get('projectName', '?')
                failures.append(f'{project}: {title}')
            elif status == 'skipped':
                skipped += 1
    # Also check nested suites
    for sub in suite.get('suites', []):
        for spec in sub.get('specs', []):
            for test in spec.get('tests', []):
                status = test.get('status', '')
                if status == 'expected':
                    passed += 1
                elif status == 'unexpected':
                    failed += 1
                    title = spec.get('title', '?')
                    project = test.get('projectName', '?')
                    failures.append(f'{project}: {title}')
                elif status == 'skipped':
                    skipped += 1
print(f'{passed}|{failed}|{skipped}')
for f in failures[:5]:
    print(f)
" 2>/dev/null)

PASSED=$(echo "$TOTAL" | head -1 | cut -d'|' -f1)
FAILED=$(echo "$TOTAL" | head -1 | cut -d'|' -f2)
SKIPPED=$(echo "$TOTAL" | head -1 | cut -d'|' -f3)
FAIL_LIST=$(echo "$TOTAL" | tail -n +2)

echo "[$TS] Passed=$PASSED Failed=$FAILED Skipped=$SKIPPED" >> "$LOG"

if [ "${FAILED:-0}" -gt 0 ]; then
    FAIL_MSG=""
    while IFS= read -r line; do
        [ -n "$line" ] && FAIL_MSG="${FAIL_MSG}
- ${line}"
    done <<< "$FAIL_LIST"

    send_telegram "🔴 *E2E Live Test — ${FAILED} HATA*
✅ ${PASSED} passed | ❌ ${FAILED} failed
${FAIL_MSG}

🕐 \`$TS\`
📋 Detay: \`/opt/linux-ai-server/e2e-live/report/\`"
else
    send_telegram "✅ *E2E Live Test — Tümü Geçti*
✅ ${PASSED} test başarılı
🌐 Renderhane, PetVet, Kuafor, Panola
🕐 \`$TS\`"
fi
