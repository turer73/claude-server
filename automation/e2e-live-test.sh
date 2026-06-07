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

# Playwright Ubuntu 26.04 (klipper host) destegi henuz yok; resmi imaj noble (24.04)
# tabanlidir ve hostta sorunsuz koser. Mount + same-user ile rapor/results.json
# dosyalari host kullanicisi olarak yazilir.
: "${E2E_PASSWORD:?Set E2E_PASSWORD in .env}"
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright:v1.60.0-noble"

docker run --rm \
  -v "$DIR:/work" \
  -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e RENDERHANE_API_KEY="$RH_KEY" \
  -e E2E_EMAIL="${E2E_EMAIL:-demo@panola.app}" \
  -e E2E_PASSWORD="$E2E_PASSWORD" \
  -e KUAFOR_PHONE="$KUAFOR_PHONE" \
  -e KUAFOR_PASS="$KUAFOR_PASS" \
  -e PETVET_PHONE="$PETVET_PHONE" \
  -e PETVET_PASS="$PETVET_PASS" \
  -e PETVET_PIN="$PETVET_PIN" \
  "$PLAYWRIGHT_IMAGE" \
  npx playwright test --grep-invert="API v1" --reporter=json 2>/dev/null > results.json
DOCKER_RC=$?

# JSON-gecerlilik + bos guard (kardes demo-reset-test.sh deseni): docker/playwright
# topyekun fail edince (VPS SSH down, image-pull, browser-crash) results.json bos/
# JSON-degil olur. Eskiden python sessizce bos doner -> FAILED="" -> 0 -gt 0 FALSE ->
# ELSE "Tumu Gecti" YESIL raporlanirdi (surer P1: tam-coku aninda sahte-yesil). Artik
# runner cokmesi FAIL'dir; testler kosmadan "gecti" demeyiz.
#
# Codex P2: SADECE results.json gecersiz/eksikse abort et — DOCKER_RC'ye BAKMA.
# Playwright test-FAIL'de de exit!=0 doner AMA gecerli JSON yazar; rc'ye gore abort
# edersek siradan test-fail'leri "CALISTIRILAMADI" sanip fail-listesini kaybederiz
# (canary teshis sinyali yok olur). rc yalnizca log/teshis icin tasinir.
if ! python3 -c "import json; json.load(open('results.json'))" 2>/dev/null; then
    echo "[$TS] E2E ABORT — docker_rc=$DOCKER_RC, results.json gecersiz/bos (parse edilemez)" >> "$LOG"
    send_telegram "🔴 *E2E Live Test — ÇALIŞTIRILAMADI*
results.json geçersiz/eksik — testler KOŞMADI (docker/playwright çöküşü, rc=${DOCKER_RC}). Sonuç güvenilmez.
🕐 \`$TS\`"
    echo "OUTCOME: fail | e2e runner cokmesi (results.json gecersiz-bos, parse edilemez; docker_rc=${DOCKER_RC}) — testler kosmadi"
    exit 1
fi

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

# Sifir-test guard: results.json gecerli-JSON ama HIC test yoksa (beklenmedik sema /
# tum-suite filtrelendi) PASSED=FAILED=SKIPPED=0 -> eskiden ELSE "Tumu Gecti" sahte-yesil.
# Hicbir test kosmadiysa bu da bir basarisizliktir.
if [ "${PASSED:-0}" -eq 0 ] && [ "${FAILED:-0}" -eq 0 ] && [ "${SKIPPED:-0}" -eq 0 ]; then
    send_telegram "🔴 *E2E Live Test — TEST YOK*
results.json geçerli ama 0 test bulundu — suite koşmadı/parse beklenmedik. Sonuç güvenilmez.
🕐 \`$TS\`"
    echo "OUTCOME: fail | results.json''da 0 test (suite kosmadi / beklenmedik sema)"
    exit 1
fi

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
    echo "OUTCOME: fail | ${FAILED} e2e test fail (${PASSED} passed)"
else
    send_telegram "✅ *E2E Live Test — Tümü Geçti*
✅ ${PASSED} test başarılı
🌐 Renderhane, PetVet, Kuafor, Panola
🕐 \`$TS\`"
    echo "OUTCOME: pass | ${PASSED} e2e test passed (${SKIPPED} skipped)"
fi
