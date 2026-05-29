#!/bin/bash
# VPS-side token refresh script (panola-social)
# Deploy to: /opt/panola-social/scripts/token-refresh-with-alert.sh
# Prerequisite: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID must be set in /opt/panola-social/.env
# Cron: replace existing token-refresh.sh call, or schedule separately
# Correction source: Note #99570 (2026-05-28) — fixes PowerShell heredoc eval issue from #99569
cd /opt/panola-social
source .env 2>/dev/null
OUTPUT=$(./venv/bin/python main.py token-auto 2>&1)
echo "$OUTPUT" | logger -t panola-token
# Alert if refresh occurred or failed
if echo "$OUTPUT" | grep -qi 'refresh\|fail\|error'; then
  MSG="IG Token Refresh: $(echo "$OUTPUT" | tail -3)"
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "text=${MSG}"
fi
