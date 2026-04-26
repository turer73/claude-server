#!/bin/bash
# PostToolUse hook (matcher: Bash) — test/lint/build sonuclarini yakalar.
# Otonom donguyi kapatir: agent test calistirir, sonuc otomatik hafizaya gider,
# bir sonraki oturum baslangicinda gorulebilir.
HOOK_NAME=post-bash-capture
. "$(dirname "$0")/lib/common.sh"

INPUT=$(cat)

EXTRACT=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
ti = d.get("tool_input") or {}
tr = d.get("tool_response") or {}
cmd = ti.get("command","")
desc = ti.get("description","")
stdout = tr.get("stdout") or tr.get("output") or ""
stderr = tr.get("stderr") or ""
rc = tr.get("exit_code")
if rc is None: rc = tr.get("returncode")
if rc is None: rc = tr.get("status")
if rc is None: rc = ""
def clip(s, n):
    s = (s or "").replace("\t"," ").replace("\r"," ")
    if len(s) > n: s = s[-n:]
    return s.replace("\n"," | ")
print(f"{cmd[:200]}\t{desc[:80]}\t{rc}\t{clip(stderr,300) or clip(stdout,300)}")
' 2>/dev/null)

[ -z "$EXTRACT" ] && exit 0

CMD=$(printf '%s' "$EXTRACT" | cut -f1)

# Yakalama tetikleyicileri — sadece anlamli komutlari kaydet
if printf '%s' "$CMD" | grep -qiE '(pytest|npm[[:space:]]+(test|run[[:space:]]+test|run[[:space:]]+build|run[[:space:]]+lint|run[[:space:]]+typecheck)|yarn[[:space:]]+(test|build|lint)|pnpm[[:space:]]+(test|build|lint)|tsc([[:space:]]|$)|ruff([[:space:]]|$)|mypy([[:space:]]|$)|eslint|cargo[[:space:]]+(test|build|check)|go[[:space:]]+(test|build)|make[[:space:]]+(test|check|build)|vitest|jest|playwright|systemctl[[:space:]]+(restart|status)|docker[[:space:]]+compose[[:space:]]+(up|down|build)|git[[:space:]]+(commit|push)|black([[:space:]]|$))'; then
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  printf '%s\t%s\n' "$TS" "$EXTRACT" >> "$HOOK_LOG_DIR/last-test-results.tsv" 2>/dev/null || true

  # Son 200 satiri tut, eskiyi at
  if [ -f "$HOOK_LOG_DIR/last-test-results.tsv" ]; then
    LINES=$(wc -l < "$HOOK_LOG_DIR/last-test-results.tsv" 2>/dev/null || echo 0)
    if [ "${LINES:-0}" -gt 200 ]; then
      tail -n 150 "$HOOK_LOG_DIR/last-test-results.tsv" > "$HOOK_LOG_DIR/last-test-results.tsv.tmp" 2>/dev/null \
        && mv "$HOOK_LOG_DIR/last-test-results.tsv.tmp" "$HOOK_LOG_DIR/last-test-results.tsv"
    fi
  fi

  # Test FAIL (rc != 0) ise discoveries'e bug olarak kaydet
  RC=$(printf '%s' "$EXTRACT" | cut -f3)
  if [ -n "$RC" ] && [ "$RC" != "0" ]; then
    # Tum dinamik veriler env var ile gecirilir — shell injection yok
    export _PROJECT="$(basename "$PWD")"
    export _DEVICE="$HOOK_DEVICE"
    export _CMD="$CMD"
    export _RC="$RC"
    export _DETAILS="$(printf '%s' "$EXTRACT" | cut -f4)"
    BODY=$(python3 -c '
import os, json
print(json.dumps({
  "device_name": os.environ.get("_DEVICE","unknown"),
  "project": os.environ.get("_PROJECT","unknown"),
  "type": "bug",
  "title": ("test-fail: " + os.environ.get("_CMD",""))[:120],
  "details": ("exit=" + os.environ.get("_RC","?") + " | " + os.environ.get("_DETAILS",""))[:1500],
  "status": "active"
}))
')
    mem_post "/discoveries" "$BODY" >/dev/null 2>&1 || true
    unset _PROJECT _DEVICE _CMD _RC _DETAILS
  fi
fi

exit 0
