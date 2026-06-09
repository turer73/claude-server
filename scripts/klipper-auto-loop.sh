#!/usr/bin/env bash
# klipper-auto-loop.sh — claude'u /loop modunda baslat
# Kullanim: bash klipper-auto-loop.sh [model]
MODEL="${1:-claude-opus-4-5}"
LOOP_CMD="/loop /opt/linux-ai-server/scripts/klipper-loop-poller.sh"

if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
    WINDOW="klipper-loop-$$"
    tmux new-window -n "$WINDOW" "claude --model $MODEL"
    sleep 4
    tmux send-keys -t "$WINDOW" "$LOOP_CMD" Enter
    tmux select-window -t "$WINDOW"
    echo "Loop session baslatildi: $WINDOW"
elif command -v expect >/dev/null 2>&1; then
    expect <(cat << EXPECTEOF
set timeout 60
log_user 1
spawn claude --model $MODEL
after 3000
send "$LOOP_CMD\r"
interact
EXPECTEOF
)
else
    echo "Manuel: claude --model $MODEL"
    echo "Sonra: $LOOP_CMD"
    exec claude --model "$MODEL"
fi
