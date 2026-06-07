#!/bin/bash
# Test post-bash-capture.sh full akis — trigger regex, SKIP_BUG kurallari, auto-resolve.
#
# Mock setup:
#   - HOOK_DB    -> mktemp sqlite (test izole)
#   - HOOK_LOG_DIR -> mktemp dizin
#   - MEMORY_API_KEY="" -> mem_post (bug acma POST'u) atlanir, WARN log
#   - Sadece sqlite yan etkileri (command_log INSERT + discoveries UPDATE) gozlenir
#
# 7 test case: trigger MISS, FP, TP-fail, TP-pass(auto-resolve), SKIP_BUG (systemctl + Test pass + echo Test:)
#
# Calistir:  bash scripts/hooks/tests/test_post_bash_capture_integration.sh
# Cikis:     0 (tum gecti) / 1 (fail var)

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/../post-bash-capture.sh"

if [ ! -x "$HOOK" ]; then
    printf 'FAIL: hook bulunamadi/exec degil: %s\n' "$HOOK" >&2
    exit 2
fi

# ---------- Setup ----------
DB=$(mktemp -t test-pbc.XXXXXX.db)
LOG_DIR=$(mktemp -d -t test-pbc-logs.XXXXXX)
cleanup() { find "$DB" -delete 2>/dev/null; find "$LOG_DIR" -depth -delete 2>/dev/null; }
trap cleanup EXIT INT TERM

# Gercek DB schema'sina uygun minimum tablolar
sqlite3 "$DB" <<'SQL'
CREATE TABLE command_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT, command TEXT, result TEXT,
    success INTEGER, created_at TEXT
);
CREATE TABLE discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER, project TEXT, type TEXT,
    title TEXT NOT NULL, details TEXT, resolved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    device_name TEXT DEFAULT 'klipper',
    status TEXT DEFAULT 'active',
    last_read_at TEXT, read_count INTEGER DEFAULT 0,
    rationale TEXT
);
SQL

# ---------- Helpers ----------
PASS=0; FAIL=0
FAILURES=()

run_hook() {
    local input="$1"
    # MEMORY_API_KEY="" + HOOK_API noop -> mem_post fail, sqlite gozle
    HOOK_DB="$DB" HOOK_LOG_DIR="$LOG_DIR" HOOK_DEVICE="test" \
        MEMORY_API_KEY="" HOOK_API="http://127.0.0.1:1/none" \
        HOOK_ENV_FILE=/dev/null \
        bash "$HOOK" <<< "$input" >/dev/null 2>&1
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        printf 'PASS  %s\n' "$label"
        PASS=$((PASS+1))
    else
        printf 'FAIL  %s\n      expected = %q\n      actual   = %q\n' "$label" "$expected" "$actual"
        FAIL=$((FAIL+1))
        FAILURES+=("$label")
    fi
}

count_cmdlog() { sqlite3 "$DB" "SELECT COUNT(*) FROM command_log"; }
count_active_bug() { sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active' AND title LIKE '%[$1]%'"; }
count_active_bug_total() { sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'"; }

# ========== TEST 1: Trigger MISS (ls -la, regex tetiklenmemeli) ==========
INPUT='{"tool_input":{"command":"ls -la /tmp"},"tool_response":{"stdout":"...","exit_code":0}}'
run_hook "$INPUT"
assert_eq "T1 trigger MISS: command_log row=0" "0" "$(count_cmdlog)"

# ========== TEST 2: Trigger HIT + FP (docker images | grep playwright) ==========
# Trigger regex 'playwright' keyword'unu gorur -> HIT. Ama CLASS=bos (FP koruyucu).
INPUT='{"tool_input":{"command":"docker images | grep playwright"},"tool_response":{"stdout":"","exit_code":1}}'
run_hook "$INPUT"
assert_eq "T2 FP: command_log row=1 (TSV+sqlite kaydedildi)" "1" "$(count_cmdlog)"
assert_eq "T2 FP: bug acilmadi (CLASS bos)" "0" "$(count_active_bug_total)"

# ========== TEST 3: Trigger HIT + TP + FAIL (npx vitest run rc=1) ==========
# CLASS=vitest, rc!=0, SKIP_BUG=0 -> bug acma DENENIR (mem_post API'ye POST).
# MEMORY_API_KEY bos -> mem_post WARN log, sqlite'a yansimaz. Bizim icin onemli olan
# command_log INSERT + auto-resolve UPDATE'in yapilmaması.
INPUT='{"tool_input":{"command":"npx vitest run"},"tool_response":{"stdout":"FAIL src/foo.test.ts","exit_code":1}}'
run_hook "$INPUT"
assert_eq "T3 TP+fail: command_log row=2" "2" "$(count_cmdlog)"
# Sahte api -> mem_post POST fail -> bug discoveries'e yazilmaz. Bu beklenen.
assert_eq "T3 TP+fail: discoveries hala 0 (API down)" "0" "$(count_active_bug_total)"

# ========== TEST 4: Trigger HIT + TP + PASS auto-resolve ==========
# Onceden sahte active vitest bug ekle, sonra rc=0 ile vitest cagir -> auto-resolve.
sqlite3 "$DB" "INSERT INTO discoveries (project, type, title, status) VALUES ('linux-ai-server', 'bug', 'test-fail [vitest]: sahte', 'active')"
assert_eq "T4 setup: 1 active vitest bug" "1" "$(count_active_bug vitest)"

# Hook \$PWD'yi project name icin kullaniyor (basename). Hook'u test'te 'linux-ai-server' dizininden cagir
cd /opt/linux-ai-server
INPUT='{"tool_input":{"command":"npx vitest run --coverage"},"tool_response":{"stdout":"50 passed","exit_code":0}}'
run_hook "$INPUT"
assert_eq "T4 TP+pass: active vitest bug auto-resolved (status=completed)" "0" "$(count_active_bug vitest)"
# Yeni eklenen bug completed olarak gorunmeli
assert_eq "T4 TP+pass: completed vitest bug=1" "1" "$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='completed' AND title LIKE '%[vitest]%'")"

# ========== TEST 5: SKIP_BUG via systemctl restart ==========
# systemctl restart rc!=0 olsa bile bug acma SKIP edilmeli.
# Onceden sahte active bug ekle ki yanlislikla auto-resolve calismadigini dogrulayabilelim.
sqlite3 "$DB" "INSERT INTO discoveries (project, type, title, status) VALUES ('linux-ai-server', 'bug', 'test-fail [vitest]: sahte2', 'active')"
INPUT='{"tool_input":{"command":"systemctl restart linux-ai-server"},"tool_response":{"stdout":"","exit_code":1}}'
run_hook "$INPUT"
# Trigger regex systemctl restart icerir, command_log INSERT olmali (audit)
assert_eq "T5 systemctl: command_log row=4 (cumulative)" "4" "$(count_cmdlog)"
# Aktif vitest bug korunmali (SKIP_BUG, auto-resolve calismadi cunku rc!=0)
assert_eq "T5 systemctl: active vitest bug hala 1" "1" "$(count_active_bug vitest)"

# ========== TEST 6: SKIP_BUG via "Test pass" in output ==========
# CMD vitest pattern'i eslesir (TP), rc!=0 ama output 'Test pass' iceriyor -> SKIP_BUG=1.
# bug acilmamali (mem_post cagrilmamali); ama mem_post cagrilsa bile API down zaten engeller -
# bu yuzden dolayli kanit: auto-resolve calismadi (rc!=0) + log'da WARN yok beklemiyoruz.
INPUT='{"tool_input":{"command":"vitest run --reporter=verbose"},"tool_response":{"stdout":"Test pass | 50 passed","exit_code":1}}'
run_hook "$INPUT"
assert_eq "T6 Test pass: command_log row=5" "5" "$(count_cmdlog)"
assert_eq "T6 Test pass: active vitest bug korunmali" "1" "$(count_active_bug vitest)"

# ========== TEST 7: SKIP_BUG via 'echo "Test:' fixture ==========
INPUT='{"tool_input":{"command":"echo \"Test: vitest fixture\""},"tool_response":{"stdout":"","exit_code":1}}'
run_hook "$INPUT"
# echo CMD trigger regex'i tetiklemeli mi? "vitest" keyword'u CMD'de var -> evet
assert_eq "T7 echo Test: command_log row=6" "6" "$(count_cmdlog)"
assert_eq "T7 echo Test: active vitest bug korunmali (SKIP_BUG)" "1" "$(count_active_bug vitest)"

# ========== TEST 8: cok-satir (composite) BASARILI komut -> auto-resolve (regresyon) ==========
# Kok neden: cmd newline icerirse TSV bozulur, cut -f3 ilk satiri rc sanar (!=0) ->
# rc=0 okunamaz -> (a) sahte test-fail bug, (b) auto-resolve tetiklenmez. Fix: cmd/desc
# tek-satira indirilir (oneline). Gozlem: cok-satir rc=0 npm build -> aktif npm bug
# auto-resolve olmali. Fix'ten ONCE bu assert FAIL ederdi (rc yanlis okunup auto-resolve atlanir).
sqlite3 "$DB" "INSERT INTO discoveries (project, type, title, status) VALUES ('linux-ai-server', 'bug', 'test-fail [npm]: sahte-multiline', 'active')"
assert_eq "T8 setup: 1 active npm bug" "1" "$(count_active_bug npm)"
INPUT='{"tool_input":{"command":"cd /data/projects/petvet\ngit checkout master\ncd web && npm run build > /tmp/x.log 2>&1 && echo build OK"},"tool_response":{"stdout":"build OK","exit_code":0}}'
run_hook "$INPUT"
assert_eq "T8 multiline rc=0: npm bug auto-resolved (fix dogrulama)" "0" "$(count_active_bug npm)"

# ========== Final ==========
printf '\n=== %d pass / %d fail (8 toplam) ===\n' "$PASS" "$FAIL"
printf '%d passed, %d failed\n' "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    printf '\nFailures:\n'
    for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
    exit 1
fi
exit 0
