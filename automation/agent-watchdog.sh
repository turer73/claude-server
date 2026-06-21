#!/usr/bin/env bash
# agent-watchdog cron wrapper (gap-7, klipper #100115 mekanizma-dışı 2. katman).
# Single-instance-lock (üst-üste binme yok) + per-run timeout (watchdog'un KENDİSİ runaway olamaz —
# ironik-koruma) + OUTCOME marker (cron_outcomes → dashboard + agent-health izler).
# AUTO_KILL .env'de set DEĞİL → default-OFF (notify-only/dry_run; FAZ-A gözlem). Salt-gözlem.
set -uo pipefail

# Tek-instance: önceki tur hâlâ koşuyorsa ATLA (fail değil — lock-held normaldir).
exec 9>/tmp/klipper-agent-watchdog.lock
if ! flock -n 9; then
    echo "OUTCOME: pass | önceki watchdog turu sürüyor, atlandı (tek-instance-lock)"
    exit 0
fi

# Per-run timeout 90s: psutil/scan hung kalırsa watchdog'u kes (kendi-runaway koruması).
out=$(timeout 90 /opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/agent-watchdog.py 2>&1)
rc=$?
echo "$out"
if [ "$rc" -eq 124 ]; then
    echo "OUTCOME: fail | watchdog 90s-timeout (kendi hung) — incele"
elif [ "$rc" -ne 0 ]; then
    echo "OUTCOME: partial | watchdog rc=$rc (import/dosya?) — ${out:0:100}"
else
    echo "OUTCOME: pass | ${out:0:120}"
fi
exit 0
