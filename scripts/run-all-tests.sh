#!/usr/bin/env bash
# ──────────────────────────────────────────────────
# Merkezi Test Runner — Tüm projeler
# Çalıştırma: bash scripts/run-all-tests.sh
# Cron:       0 6 * * * /opt/linux-ai-server/automation/test-runner.sh
# ──────────────────────────────────────────────────

set -euo pipefail

RESULTS_FILE="/tmp/test-results-$(date +%Y%m%d-%H%M%S).json"
COVERAGE_DB="/opt/linux-ai-server/data/coverage.db"
FAILED=0
TOTAL_TESTS=0
TOTAL_PASSED=0
TOTAL_FAILED=0
PROJECTS=()
DETAILS=""

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Git Sync ──────────────────────────────────────

sync_repos() {
  log "═══ Git Sync ═══"
  local repos=(
    "/data/projects/panola"
    "/data/projects/bilge-arena"
    "/data/projects/renderhane"
    "/data/projects/koken-akademi"
    "/data/projects/kuafor"
    "/data/projects/petvet"
    "/data/projects/demo-saas"
  )
  for repo in "${repos[@]}"; do
    if [ -d "$repo/.git" ]; then
      local name=$(basename "$repo")
      local pull_out
      pull_out=$(cd "$repo" && git fetch origin && git reset --hard origin/master 2>&1) || true
      if echo "$pull_out" | grep -q "HEAD is now at"; then
        log "  ↓ $name — güncellendi"
      else
        log "  · $name — güncel"
      fi
    fi
  done
  echo ""
}

# ── Dependency Audit ──────────────────────────────

dep_audit() {
  log "═══ Dependency Audit ═══"
  local vuln_total=0
  local audit_details=""

  local node_projects=(
    "panola:/data/projects/panola"
    "bilge-arena:/data/projects/bilge-arena"
    "renderhane:/data/projects/renderhane"
    "petvet-web:/data/projects/petvet/web"
  )

  for entry in "${node_projects[@]}"; do
    IFS=: read -r name dir <<< "$entry"
    if [ -f "$dir/package-lock.json" ]; then
      local audit_out high=0 critical=0
      audit_out=$(cd "$dir" && npm audit --json --omit=dev 2>/dev/null) || true
      high=$(echo "$audit_out" | grep -oP '"high":\s*\K\d+' | head -1 || echo 0)
      critical=$(echo "$audit_out" | grep -oP '"critical":\s*\K\d+' | head -1 || echo 0)
      local total=$(( ${high:-0} + ${critical:-0} ))
      vuln_total=$((vuln_total + total))

      if [ "$total" -gt 0 ]; then
        log "  ⚠ $name — $critical critical, $high high"
        audit_details+="$name: ${critical}C/${high}H | "
      else
        log "  ✓ $name — temiz"
      fi
    fi
  done

  AUDIT_VULNS=$vuln_total
  AUDIT_DETAILS="${audit_details%| }"
  echo ""
}

# ── Test Runner ───────────────────────────────────

run_project() {
  local name="$1"
  local dir="$2"
  local cmd="$3"
  local tests=0 passed=0 failed=0 status="pass" output=""

  log "▶ $name"

  if [ ! -d "$dir" ]; then
    log "  ⚠ Dizin bulunamadı: $dir"
    DETAILS+="\"$name\":{\"status\":\"skip\",\"reason\":\"directory not found\"},"
    return
  fi

  output=$(cd "$dir" && eval "$cmd" 2>&1) || true
  local exit_code=${PIPESTATUS[0]:-$?}

  # Parse test counts
  if echo "$output" | grep -qE '[0-9]+ passed'; then
    passed=$(echo "$output" | grep -oE '[0-9]+ passed' | tail -1 | grep -oE '[0-9]+')
    tests=$((tests + passed))
  fi
  if echo "$output" | grep -qE '[0-9]+ failed'; then
    failed=$(echo "$output" | grep -oE '[0-9]+ failed' | tail -1 | grep -oE '[0-9]+')
    tests=$((tests + failed))
  fi

  if [ "${failed:-0}" -gt 0 ] || [ "$exit_code" -ne 0 ]; then
    status="fail"
    FAILED=1
    log "  ✗ FAIL — $passed passed, $failed failed"
  else
    log "  ✓ PASS — $passed passed"
  fi

  TOTAL_TESTS=$((TOTAL_TESTS + ${tests:-0}))
  TOTAL_PASSED=$((TOTAL_PASSED + ${passed:-0}))
  TOTAL_FAILED=$((TOTAL_FAILED + ${failed:-0}))
  PROJECTS+=("$name:$status:${passed:-0}:${failed:-0}")
  DETAILS+="\"$name\":{\"status\":\"$status\",\"passed\":${passed:-0},\"failed\":${failed:-0}},"
}

# ── Coverage Tracking ─────────────────────────────

save_coverage() {
  # SQLite'a kaydet — trend takibi için
  if command -v sqlite3 &>/dev/null; then
    sqlite3 "$COVERAGE_DB" "
      CREATE TABLE IF NOT EXISTS test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        total_tests INTEGER,
        total_passed INTEGER,
        total_failed INTEGER,
        status TEXT,
        details TEXT
      );
      INSERT INTO test_runs (timestamp, total_tests, total_passed, total_failed, status, details)
      VALUES ('$(date -Iseconds)', $TOTAL_TESTS, $TOTAL_PASSED, $TOTAL_FAILED,
              '$([ $FAILED -eq 0 ] && echo pass || echo fail)',
              '{${DETAILS%,}}');
    " 2>/dev/null || true
    log "Coverage DB güncellendi"
  fi
}

# ── Telegram Bildirim ─────────────────────────────

send_telegram() {
  local TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  local TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    # n8n webhook fallback
    local WEBHOOK_URL="${TEST_WEBHOOK_URL:-}"
    if [ -n "$WEBHOOK_URL" ]; then
      curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d @"$RESULTS_FILE" > /dev/null 2>&1 || true
      log "n8n webhook gönderildi"
    fi
    return
  fi

  local emoji="✅"
  local status_text="BAŞARILI"
  if [ $FAILED -ne 0 ]; then
    emoji="🚨"
    status_text="BAŞARISIZ"
  fi

  # Bir projenin pesi sira fail streak'ini coverage.db'den oku.
  # save_coverage send_telegram sonrasi cagrilir; bu yuzden son run henuz tabloda yok.
  consecutive_fails() {
    local proj="$1"
    sqlite3 "$COVERAGE_DB" "
      SELECT IFNULL(json_extract(details,'\$.\"${proj}\".status'),'absent')
      FROM test_runs ORDER BY id DESC LIMIT 30;" 2>/dev/null \
    | awk '
        /^fail$/ { c++; next }
        /^pass$/ { print c+0; exit }
        /^absent$/ { print c+0; exit }
        END { print c+0 }'
  }

  local failed_list=""
  local persistent_count=0
  for p in "${PROJECTS[@]}"; do
    IFS=: read -r name status passed failed_count <<< "$p"
    if [ "$status" = "fail" ]; then
      local streak
      streak=$(consecutive_fails "$name")
      streak=$((${streak:-0} + 1))  # +1: bu run henuz DB'de degil
      local tag=""
      if [ "$streak" -ge 3 ]; then
        tag=" 🚨 PERSISTENT (${streak}g)"
        persistent_count=$((persistent_count + 1))
      fi
      failed_list+="  ✗ $name ($failed_count failed)${tag}
"
    fi
  done

  # Persistent varsa baslik emojisini cevir (kalici problem != flaky)
  if [ "$persistent_count" -gt 0 ]; then
    emoji="🚨🚨"
    status_text="KALICI BAŞARISIZ ($persistent_count proje)"
  fi

  local msg="$emoji *Test Runner — $status_text*

📊 Toplam: $TOTAL_TESTS test
✅ Geçen: $TOTAL_PASSED
❌ Başarısız: $TOTAL_FAILED"

  if [ -n "$failed_list" ]; then
    msg+="

🔴 *Kırık Projeler:*
$failed_list"
  fi

  if [ "${AUDIT_VULNS:-0}" -gt 0 ]; then
    msg+="
⚠️ *Güvenlik:* $AUDIT_VULNS zafiyet ($AUDIT_DETAILS)"
  fi

  msg+="
🕐 $(date '+%H:%M %d/%m/%Y')"

  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" \
    -d parse_mode="Markdown" \
    -d text="$msg" > /dev/null 2>&1 || true

  log "Telegram bildirim gönderildi"
}

# ══════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════

log "═══ Merkezi Test Runner ═══"
log "Tarih: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 1) Git sync
sync_repos

# 2) Dependency audit
AUDIT_VULNS=0
AUDIT_DETAILS=""
dep_audit

# 3) Testleri çalıştır
log "═══ Testler ═══"

run_project "linux-ai-server" \
  "/opt/linux-ai-server" \
  "source /opt/linux-ai-server/venv/bin/activate && python -m pytest tests/ -x -q --tb=line 2>&1"

run_project "panola" \
  "/data/projects/panola" \
  "npx vitest run 2>&1"

run_project "bilge-arena" \
  "/data/projects/bilge-arena" \
  "npx vitest run 2>&1"

run_project "renderhane" \
  "/data/projects/renderhane" \
  "npx vitest run 2>&1"

run_project "koken-akademi" \
  "/data/projects/koken-akademi/apps/api" \
  "npx vitest run 2>&1"

run_project "kuafor-worker" \
  "/data/projects/kuafor/worker" \
  "npx vitest run 2>&1"

run_project "kuafor-panel" \
  "/data/projects/kuafor/panel" \
  "npx vitest run 2>&1"

run_project "petvet-web" \
  "/data/projects/petvet/web" \
  "npx vitest run 2>&1"

# ── Sonuç Özeti ───────────────────────────────────

echo ""
log "═══ Sonuç ═══"
log "Toplam: $TOTAL_TESTS test | $TOTAL_PASSED geçti | $TOTAL_FAILED başarısız"

for p in "${PROJECTS[@]}"; do
  IFS=: read -r name status passed failed <<< "$p"
  if [ "$status" = "pass" ]; then
    log "  ✓ $name ($passed passed)"
  else
    log "  ✗ $name ($passed passed, $failed failed)"
  fi
done

# ── JSON çıktı ────────────────────────────────────

DETAILS="${DETAILS%,}"
cat > "$RESULTS_FILE" <<EOF
{
  "timestamp": "$(date -Iseconds)",
  "total_tests": $TOTAL_TESTS,
  "total_passed": $TOTAL_PASSED,
  "total_failed": $TOTAL_FAILED,
  "status": "$([ $FAILED -eq 0 ] && echo 'pass' || echo 'fail')",
  "audit_vulns": ${AUDIT_VULNS:-0},
  "projects": {$DETAILS}
}
EOF

log "Sonuçlar: $RESULTS_FILE"

# 4) Coverage DB'ye kaydet
save_coverage

# 5) Bildirim gönder (hata varsa veya haftalık özet)
if [ $FAILED -ne 0 ] || [ "${FORCE_NOTIFY:-}" = "1" ]; then
  send_telegram
fi

exit $FAILED
