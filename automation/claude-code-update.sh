#!/bin/bash
# claude-code-update.sh — @anthropic-ai/claude-code haftalik global guncelleme.
# /usr/lib/node_modules altinda root yetkisi gerekiyor, NOPASSWD ile sudo cagrilir.

set -euo pipefail

LOG_DIR=/var/log/linux-ai-server
mkdir -p "$LOG_DIR" 2>/dev/null || sudo mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/claude-code-update.log"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

PKG=/usr/lib/node_modules/@anthropic-ai/claude-code/package.json
BEFORE=$(jq -r .version "$PKG" 2>/dev/null || grep -oP '"version":\s*"\K[^"]+' "$PKG" 2>/dev/null || echo "unknown")
LATEST=$(npm view @anthropic-ai/claude-code version 2>/dev/null || echo "unknown")

echo "[$(ts)] start — installed=$BEFORE latest=$LATEST" >> "$LOG"

if [ "$BEFORE" = "$LATEST" ] && [ "$BEFORE" != "unknown" ]; then
    echo "[$(ts)] already up to date ($BEFORE) — skip" >> "$LOG"
    exit 0
fi

sudo -n npm i -g @anthropic-ai/claude-code >> "$LOG" 2>&1
RC=$?

AFTER=$(jq -r .version "$PKG" 2>/dev/null || grep -oP '"version":\s*"\K[^"]+' "$PKG" 2>/dev/null || echo "unknown")
echo "[$(ts)] done rc=$RC — $BEFORE -> $AFTER" >> "$LOG"

exit $RC
