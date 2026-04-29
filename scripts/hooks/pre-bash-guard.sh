#!/bin/bash
# PreToolUse hook (matcher: Bash) — yikici komutlari engeller, kullaniciyi escalation'a zorlar.
# Cikti protokolu (Claude Code):
#   exit 0  -> komut gecsin
#   exit 2  -> stderr'i gosterip komutu BLOKLA (Claude'a uyari donsun)
HOOK_NAME=pre-bash-guard
. "$(dirname "$0")/lib/common.sh"

INPUT=$(cat)

CMD=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print((d.get("tool_input") or {}).get("command",""))
except Exception:
    sys.exit(0)
' 2>/dev/null)

[ -z "$CMD" ] && exit 0

# Yikici desenler
DANGEROUS_PATTERNS=(
  'rm[[:space:]]+-[a-zA-Z]*r[a-zA-Z]*f'
  'rm[[:space:]]+-[a-zA-Z]*f[a-zA-Z]*r'
  ':\(\)\{[[:space:]]*:\|:&[[:space:]]*\};:'
  'mkfs\.'
  'dd[[:space:]]+if=.*of=/dev/(sd|nvme|hd)'
  '>[[:space:]]*/dev/(sd|nvme|hd)'
  'shutdown[[:space:]]+'
  'reboot([[:space:]]|$)'
  'halt([[:space:]]|$)'
  'poweroff([[:space:]]|$)'
  'systemctl[[:space:]]+(stop|disable|mask)[[:space:]]+(linux-ai-server|ssh|sshd|networking|systemd-resolved)'
  'iptables[[:space:]]+-F'
  'ufw[[:space:]]+(disable|reset)'
  'git[[:space:]]+push.*--force'
  'git[[:space:]]+push.*[[:space:]]+-f([[:space:]]|$)'
  'git[[:space:]]+reset[[:space:]]+--hard'
  'git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*[fdx]'
  'git[[:space:]]+branch[[:space:]]+-D'
  'docker[[:space:]]+(system[[:space:]]+prune|volume[[:space:]]+rm|rm[[:space:]]+-f)'
  'DROP[[:space:]]+(TABLE|DATABASE|SCHEMA)'
  'TRUNCATE[[:space:]]+TABLE'
  'curl[[:space:]]+.*\|[[:space:]]*(bash|sh|zsh)([[:space:]]|$)'
  'wget[[:space:]]+.*-O-.*\|[[:space:]]*(bash|sh)'
  'chmod[[:space:]]+-R[[:space:]]+777[[:space:]]+/'
  'chown[[:space:]]+-R.*[[:space:]]+/[[:space:]]'
)

for pat in "${DANGEROUS_PATTERNS[@]}"; do
  if printf '%s' "$CMD" | grep -qE "$pat"; then
    if [ "${HOOK_AUTONOMY:-supervised}" = "autonomous" ] && [ -n "${HOOK_DESTRUCTIVE_ACK:-}" ]; then
      hook_log "OTONOM BYPASS: $CMD (pattern: $pat)"
      exit 0
    fi
    hook_log "BLOK: $CMD (pattern: $pat)"
    {
      echo "PRE-BASH-GUARD: bu komut yikici islem icerir ve onay gerekir."
      echo "Eslestigi desen: $pat"
      echo "Komut: $CMD"
      echo ""
      echo "Karar:"
      echo "  - Gercekten yapilmasi gerekiyorsa kullaniciya nedenini aciklayip onay iste."
      echo "  - Onay alindiktan sonra HOOK_DESTRUCTIVE_ACK=1 ortam degiskenini set ederek tekrar dene."
      echo "  - Otonom modda calisiyorsan: HOOK_DESTRUCTIVE_ACK=1 set edilmedikce calistirma."
    } >&2
    exit 2
  fi
done

exit 0
