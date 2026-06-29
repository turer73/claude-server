#!/bin/bash
NOTE_ID=${1:-100101}
KEY=$(grep "^MEMORY_API_KEY=" /opt/linux-ai-server/.env | head -1 | sed "s/^MEMORY_API_KEY=//")
curl -s -X PUT "http://127.0.0.1:8420/api/v1/memory/notes/${NOTE_ID}/read?device=klipper" -H "X-Memory-Key: ${KEY}"
echo
