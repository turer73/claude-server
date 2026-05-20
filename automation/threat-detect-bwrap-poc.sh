#!/bin/bash
# threat-detect-bwrap-poc.sh — bwrap POC for autonomous-spawn-threat-detect
#
# AMAC: bwrap'in autonomous spawn flow'unda kullanilirsa nasil gorunecegini
# kanitla. Production'a sokulmaz — sadece POC.
#
# Bu wrapper threat-detect.sh'yi izole bir bwrap mount namespace icinde
# calistirir. FS gorunurlugu sadece gerekli minimal path'lere kisitlanir.
#
# Olculen:
#   - Startup latency (bare exec vs bwrap)
#   - FS policy: hangi path'lerin acik olmasi gerekti
#   - Net policy: 127.0.0.1 API erisimi gerekiyor mu (telegram + memory POST)
#
# Kullanim: ./threat-detect-bwrap-poc.sh <SPAWN_LOG_PATH> [--bare|--bwrap]

set -uo pipefail

MODE="${2:-bwrap}"
SPAWN_LOG="${1:-/tmp/poc-spawn-log.txt}"
THREAT_SCRIPT="/opt/linux-ai-server/automation/autonomous-spawn-threat-detect.sh"
NOTE_ID="9000"  # POC test note id

if [ ! -f "$SPAWN_LOG" ]; then
    echo "Usage: $0 <SPAWN_LOG> [--bare|--bwrap]"
    exit 1
fi

case "$MODE" in
    --bare|bare)
        # Baseline — host'ta calistir, izolasyon yok
        exec "$THREAT_SCRIPT" "$NOTE_ID" "$SPAWN_LOG"
        ;;
    --bwrap|bwrap)
        # bwrap policy — minimum FS gorunurlugu
        # FS allowlist:
        #   /usr, /lib*, /etc/resolv.conf — binary + resolver (read-only)
        #   /etc/passwd, /etc/group, /etc/nsswitch.conf — id lookup
        #   /opt/linux-ai-server/automation — scripts (telegram-alert dahil, read-only)
        #   /opt/linux-ai-server/.env.autonomous — MEMORY_API_KEY (read-only)
        #   $SPAWN_LOG — incelenecek log (read-only)
        #   /opt/linux-ai-server/data/hook-logs — kendi log'unu append edebilir
        #   /tmp — tmpfs (private)
        # Net: --share-net (127.0.0.1 API + telegram.org gerek)
        # User: same uid (no userns remap — privilege drop separate concern)
        exec bwrap \
            --ro-bind /usr /usr \
            --ro-bind /lib /lib \
            --ro-bind /lib64 /lib64 \
            --ro-bind /bin /bin \
            --ro-bind /sbin /sbin \
            --ro-bind /etc/resolv.conf /etc/resolv.conf \
            --ro-bind /etc/passwd /etc/passwd \
            --ro-bind /etc/group /etc/group \
            --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
            --ro-bind /etc/ssl /etc/ssl \
            --ro-bind /etc/ca-certificates /etc/ca-certificates \
            --ro-bind /opt/linux-ai-server/automation /opt/linux-ai-server/automation \
            --ro-bind /opt/linux-ai-server/.env.autonomous /opt/linux-ai-server/.env.autonomous \
            --bind /opt/linux-ai-server/data/hook-logs /opt/linux-ai-server/data/hook-logs \
            --tmpfs /tmp \
            --ro-bind "$SPAWN_LOG" "$SPAWN_LOG" \
            --proc /proc \
            --dev /dev \
            --share-net \
            --unshare-pid \
            --unshare-ipc \
            --unshare-uts \
            --unshare-cgroup \
            --die-with-parent \
            --new-session \
            --chdir /opt/linux-ai-server \
            --setenv HOOK_ENV_FILE /opt/linux-ai-server/.env.autonomous \
            --setenv TELEGRAM_ENV_FILE /opt/linux-ai-server/.env.autonomous \
            --setenv PATH "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
            -- \
            "$THREAT_SCRIPT" "$NOTE_ID" "$SPAWN_LOG"
        ;;
    *)
        echo "Mode: bare | bwrap (got: $MODE)"
        exit 1
        ;;
esac
