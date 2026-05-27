#!/bin/bash
# Test scripts/hooks/lib/classify-cmd.sh
#
# Memory ref: fix_hook_class_regex_2026_05_27 (commit cfbaf1e)
# Bug ref:    #485, #499, #500 — salt-keyword glob FP (post-bash-capture.sh)
#
# 14 case: 9 TP (gercek runner cagri tanindi) + 5 FP (salt grep/find/ls/docker = CLASS bos)
#
# Calistir:
#   bash scripts/hooks/tests/test_classify_cmd.sh
# Cikis kodu: 0 (hepsi gecer) / 1 (fail var)

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../lib/classify-cmd.sh"

if [ ! -f "$LIB" ]; then
    printf 'FAIL: lib bulunamadi: %s\n' "$LIB" >&2
    exit 2
fi

. "$LIB"

PASS=0
FAIL=0
FAILURES=()

assert_class() {
    local label="$1" cmd="$2" expected="$3"
    local actual
    actual=$(classify_cmd "$cmd")
    if [ "$actual" = "$expected" ]; then
        printf 'PASS  %s\n' "$label"
        PASS=$((PASS+1))
    else
        printf 'FAIL  %s\n      cmd      = %s\n      expected = %q\n      actual   = %q\n' "$label" "$cmd" "$expected" "$actual"
        FAIL=$((FAIL+1))
        FAILURES+=("$label")
    fi
}

# ---------- TP (9 case): gercek runner cagrilari, dogru CLASS atanmali ----------
assert_class "TP1  npx vitest run"          "npx vitest run"                       "vitest"
assert_class "TP2  vitest run --coverage"   "vitest run --coverage"                "vitest"
assert_class "TP3  pnpm vitest"             "pnpm vitest"                          "vitest"
assert_class "TP4  npx jest"                "npx jest"                             "jest"
assert_class "TP5  jest --watch"            "jest --watch"                         "jest"
assert_class "TP6  npx playwright test"     "npx playwright test"                  "playwright"
assert_class "TP7  playwright install"      "playwright install --with-deps"       "playwright"
assert_class "TP8  npx eslint ."            "npx eslint ."                         "eslint"
assert_class "TP9  eslint --fix src/"       "eslint --fix src/"                    "eslint"

# ---------- FP (5 case): salt-keyword grep/find/ls/docker — CLASS bos olmali ----------
assert_class "FP1  docker images grep playwright"  "docker images | grep playwright"   ""
assert_class "FP2  grep -r vitest ./src"           "grep -r vitest ./src"              ""
assert_class "FP3  find -name jest"                "find . -name '*jest*'"             ""
assert_class "FP4  ls node_modules grep eslint"    "ls node_modules/ | grep eslint"    ""
assert_class "FP5  cat grep playwright"            "cat package.json | grep playwright" ""

printf '\n=== %d pass / %d fail (14 toplam) ===\n' "$PASS" "$FAIL"

# Pytest-uyumlu satir — run-all-tests.sh parser'i icin (regex: [0-9]+ passed, [0-9]+ failed)
printf '%d passed, %d failed\n' "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    printf '\nFailures:\n'
    for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
    exit 1
fi
exit 0
