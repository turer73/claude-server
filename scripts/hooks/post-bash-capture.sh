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
if printf '%s' "$CMD" | grep -qiE '(pytest|npm[[:space:]]+(test|run[[:space:]]+test|run[[:space:]]+build|run[[:space:]]+lint|run[[:space:]]+typecheck)|yarn[[:space:]]+(test|build|lint)|pnpm[[:space:]]+(test|build|lint)|tsc([[:space:]]|$)|ruff([[:space:]]|$)|mypy([[:space:]]|$)|eslint|cargo[[:space:]]+(test|build|check)|go[[:space:]]+(test|build)|make[[:space:]]+(test|check|build)|vitest|jest|playwright|systemctl[[:space:]]+(restart|status)|docker[[:space:]]+compose[[:space:]]+(up|down|build)|black([[:space:]]|$))'; then
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  printf '%s\t%s\n' "$TS" "$EXTRACT" >> "$HOOK_LOG_DIR/last-test-results.tsv" 2>/dev/null || true

  # command_log tablosuna kaydet (sqlite3 direkt — API bagimliligi yok)
  RC_VAL=$(printf '%s' "$EXTRACT" | cut -f3)
  RESULT_VAL=$(printf '%s' "$EXTRACT" | cut -f4)
  SUCCESS_VAL=1
  [ -n "$RC_VAL" ] && [ "$RC_VAL" != "0" ] && SUCCESS_VAL=0
  sqlite3 "$HOOK_DB" "INSERT INTO command_log (device_name, command, result, success, created_at) VALUES ('$HOOK_DEVICE', '$(printf '%s' "$CMD" | sed "s/'/''/g")', '$(printf '%s' "$RESULT_VAL" | head -c 500 | sed "s/'/''/g")', $SUCCESS_VAL, '$TS');" 2>/dev/null || true

  # Son 200 satiri tut, eskiyi at
  if [ -f "$HOOK_LOG_DIR/last-test-results.tsv" ]; then
    LINES=$(wc -l < "$HOOK_LOG_DIR/last-test-results.tsv" 2>/dev/null || echo 0)
    if [ "${LINES:-0}" -gt 200 ]; then
      tail -n 150 "$HOOK_LOG_DIR/last-test-results.tsv" > "$HOOK_LOG_DIR/last-test-results.tsv.tmp" 2>/dev/null \
        && mv "$HOOK_LOG_DIR/last-test-results.tsv.tmp" "$HOOK_LOG_DIR/last-test-results.tsv"
    fi
  fi

  # Test class detection — composite cmd'lerde regex eşleşen alt-komutu sınıfla.
  # Trigger regex bug açarken VE rc=0 sonrası resolve ederken aynı sınıfı
  # vermeli — title'a [class] etiketi koyup ona göre eşleşelim. Etiketsiz
  # bug açma (class boş ise) — keyword'ün rastgele bir sub-string olarak
  # geçtiği composite komutta yanlış pozitif üretir, atlıyoruz.
  CLASS=""
  case "$CMD" in
    *pytest*)                                                                                  CLASS="pytest" ;;
    *"npm test"*|*"npm run test"*|*"npm run build"*|*"npm run lint"*|*"npm run typecheck"*)    CLASS="npm" ;;
    *"yarn test"*|*"yarn build"*|*"yarn lint"*)                                                CLASS="yarn" ;;
    *"pnpm test"*|*"pnpm build"*|*"pnpm lint"*|*"pnpm exec tsc"*|*"pnpm typecheck"*)           CLASS="pnpm" ;;
    *" tsc "*|*" tsc"|"tsc "*|"tsc")                                                           CLASS="tsc" ;;
    *" ruff "*|*" ruff"|"ruff "*|"ruff")                                                       CLASS="ruff" ;;
    *" mypy "*|*" mypy"|"mypy "*|"mypy")                                                       CLASS="mypy" ;;
    *vitest*)                                                                                  CLASS="vitest" ;;
    *jest*)                                                                                    CLASS="jest" ;;
    *playwright*)                                                                              CLASS="playwright" ;;
    *eslint*)                                                                                  CLASS="eslint" ;;
    *"cargo test"*|*"cargo build"*|*"cargo check"*)                                            CLASS="cargo" ;;
    *"go test"*|*"go build"*)                                                                  CLASS="go" ;;
    *"make test"*|*"make check"*|*"make build"*)                                               CLASS="make" ;;
    *" black "*|"black "*)                                                                     CLASS="black" ;;
  esac

  # Test FAIL (rc != 0) ise discoveries'e bug olarak kaydet —
  # ama systemctl restart/status komutlarinda atla. Servis restart olunca
  # zincirdeki curl /health vs adimlari henuz acilmamis servise dustugu
  # icin rc!=0 oluyor; bu bir test regression degil, beklenen yarış. TSV
  # log + command_log audit'i zaten yapildi, sadece bug-spam'i durdur.
  RC=$(printf '%s' "$EXTRACT" | cut -f3)
  SKIP_BUG=0
  if printf '%s' "$CMD" | grep -qE 'systemctl[[:space:]]+(restart|status)'; then
    SKIP_BUG=1
  fi
  # False-positive koruma: composite shell line'da (echo + ssh + git + gh ...)
  # son alt-komutun rc'si fail olabilir ama test runner cikti SUCCESS sinyali
  # tasiyor olabilir. Cikti icinde explicit pass keyword'leri varsa SKIP.
  # Bug #465 + #466 root cause — vitest "Test pass" + gh "mergeStateStatus CLEAN"
  # composite line'in sonundaki ssh fail nedeniyle false bug aciliyor.
  DETAILS_PEEK=$(printf '%s' "$EXTRACT" | cut -f4 | head -c 4000)
  if printf '%s' "$DETAILS_PEEK" | grep -qE 'Test pass[[:space:]]|all tests passed|[0-9]+[[:space:]]+passed|0 failing|mergeStateStatus["][[:space:]:]*["]CLEAN["]'; then
    SKIP_BUG=1
  fi
  # Self-test/fixture probe — 'echo "Test: ..."' ile baslayan composite komutlar
  # hook'un kendi normalize/regex davranisini test eden fixture'lardir; gerçek
  # test runner cagrisi degil. Bug #483 vakasi: cwd=linux-ai-server iken composite
  # cmd icinde 'vitest' kelimesi gectigi icin CLASS=vitest tagged → linux-ai-server'da
  # vitest hic kosmadigi icin auto-resolve asla tetiklenmez, kalici sahte bug.
  if printf '%s' "$CMD" | grep -qE "^[[:space:]]*echo[[:space:]]+['\"]Test:"; then
    SKIP_BUG=1
  fi
  # Class boş ise bug açma — regex'in rastgele match ettiği composite komut.
  if [ -n "$RC" ] && [ "$RC" != "0" ] && [ "$SKIP_BUG" = "0" ] && [ -n "$CLASS" ]; then
    # Tum dinamik veriler env var ile gecirilir — shell injection yok
    export _PROJECT="$(basename "$PWD")"
    export _DEVICE="$HOOK_DEVICE"
    export _CMD="$CMD"
    export _RC="$RC"
    export _CLASS="$CLASS"
    export _DETAILS="$(printf '%s' "$EXTRACT" | cut -f4)"
    BODY=$(python3 -c '
import os, json, re
cmd = os.environ.get("_CMD","")
rc = os.environ.get("_RC","?")
cls = os.environ.get("_CLASS","")
# Title icin komutu normalize: ilk satir, ilk sub-command, whitespace collapse
# Why: multi-line/heredoc/composite komutlar title alanini bozuyordu (#482 vakasi)
cmd_title = cmd.split("\n", 1)[0]
for sep in ("&&", ";", "||", "|"):
    cmd_title = cmd_title.split(sep, 1)[0]
cmd_title = re.sub(r"\s+", " ", cmd_title).strip()
print(json.dumps({
  "device_name": os.environ.get("_DEVICE","unknown"),
  "project": os.environ.get("_PROJECT","unknown"),
  "type": "bug",
  "title": (f"test-fail [{cls}]: " + cmd_title)[:120],
  "details": ("exit=" + rc + " | " + os.environ.get("_DETAILS",""))[:1500],
  "status": "active",
  "rationale": (f"PostToolUse-hook auto-capture on klipper. Command class={cls} exited rc=" + rc + " — treated as test/lint/build regression. Auto-resolves on next rc=0 run of the same class in the same project; title tag [{cls}] keeps the match exact even when the failing sub-command is buried in a composite shell line.")[:500]
}))
')
    mem_post "/discoveries" "$BODY" >/dev/null 2>&1 || true
    unset _PROJECT _DEVICE _CMD _RC _CLASS _DETAILS
  fi

  # rc=0 ise: aynı projede aynı sınıftaki eski "test-fail [class]:" bug'larını
  # auto-resolve et. Etiket sayesinde title'da [class] aramak yeterli — composite
  # komut nedeniyle title'da class keyword'ü bulunmasa bile etiket korunur.
  # Risk: tek dosya geçerse genel class bug'ı da yanlış kapanır; ama gerçek
  # regression bir sonraki çalıştırmada yeniden açılır (POST dedupe-on-active).
  if [ "${RC:-1}" = "0" ] && [ "$SKIP_BUG" = "0" ] && [ -n "$CLASS" ]; then
    PROJECT_NAME="$(basename "$PWD" | sed "s/'/''/g")"
    CLASS_TAG="[$CLASS]"
    CLASS_ESC="$(printf '%s' "$CLASS_TAG" | sed "s/'/''/g")"
    sqlite3 "$HOOK_DB" "UPDATE discoveries
      SET resolved=1, status='completed'
      WHERE project='$PROJECT_NAME'
        AND type='bug'
        AND status='active'
        AND title LIKE 'test-fail%'
        AND title LIKE '%' || '$CLASS_ESC' || '%'" 2>/dev/null || true
  fi
fi

exit 0
