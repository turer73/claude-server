#!/bin/bash
# Daily automated backup
API=http://localhost:8420
KEY=REDACTED_API_KEY
TOKEN=$(curl -s -X POST $API/api/v1/auth/token -H 'Content-Type: application/json' -d "{\"api_key\": \"$KEY\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

LABEL="auto-$(date +%Y%m%d-%H%M)"
RESULT=$(curl -s -X POST $API/api/v1/backup/create   -H "Authorization: Bearer $TOKEN"   -H 'Content-Type: application/json'   -d "{\"label\": \"$LABEL\"}")

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[$TIMESTAMP] Backup '$LABEL': $RESULT" >> /var/log/linux-ai-server/backup.log

# Cleanup: keep only last 7 backups
ls -t /var/lib/linux-ai-server/backups/*.tar.gz 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null
