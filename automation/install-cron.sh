#!/bin/bash
# Install / update the linux-ai-server crontab from automation/crontab.
#
# Idempotent: replaces the user's crontab with the file contents. If a
# crontab already exists it is backed up to ~/.crontab.bak.<timestamp>
# first so a mistake is recoverable.
#
# Usage: bash automation/install-cron.sh
#        DRY_RUN=1 bash automation/install-cron.sh   # show diff, don't apply

set -euo pipefail

CRONTAB_FILE=${CRONTAB_FILE:-/opt/linux-ai-server/automation/crontab}
DRY_RUN=${DRY_RUN:-0}

if [ ! -f "$CRONTAB_FILE" ]; then
    echo "ERROR: $CRONTAB_FILE not found" >&2
    exit 1
fi

echo "Source: $CRONTAB_FILE ($(grep -cE '^[^#]' "$CRONTAB_FILE") active entries)"

if crontab -l >/dev/null 2>&1; then
    current=$(mktemp)
    crontab -l > "$current"
    if diff -q "$current" "$CRONTAB_FILE" >/dev/null; then
        echo "Crontab already matches — nothing to do."
        rm -f "$current"
        exit 0
    fi
    echo ""
    echo "Diff against current crontab:"
    diff -u "$current" "$CRONTAB_FILE" || true
    if [ "$DRY_RUN" = "1" ]; then
        rm -f "$current"
        echo ""
        echo "DRY_RUN=1 — not applying."
        exit 0
    fi
    backup="$HOME/.crontab.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$current" "$backup"
    echo ""
    echo "Backed up current crontab to $backup"
    rm -f "$current"
else
    echo "No existing crontab for $(whoami) — installing fresh."
    if [ "$DRY_RUN" = "1" ]; then
        cat "$CRONTAB_FILE"
        echo ""
        echo "DRY_RUN=1 — not applying."
        exit 0
    fi
fi

crontab "$CRONTAB_FILE"
echo "Crontab installed. Verify with: crontab -l"
