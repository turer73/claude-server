#!/bin/bash
# PSOC-20260529-01: webhook_server.py subprocess timeout 300 -> 600
# Deploy via: scripts/vps-run.sh < infra/panola-social-patches/scripts/psoc-20260529-01-webhook-timeout.sh
# Sonra servis restart: scripts/vps-run.sh "cd /opt/panola-social && supervisorctl restart webhook || systemctl restart panola-social-webhook 2>/dev/null || pkill -f webhook_server && nohup python webhook_server.py &"
set -e

TARGET="/opt/panola-social/webhook_server.py"

if [ ! -f "$TARGET" ]; then
  echo "ERROR: $TARGET bulunamadı" >&2
  exit 1
fi

# Mevcut satırı göster
echo "=== Önce ==="
grep -n "timeout=300" "$TARGET" || echo "(timeout=300 bulunamadı — zaten değiştirilmiş olabilir)"

# Yedeği al
cp "$TARGET" "${TARGET}.bak-pre-psoc20260529-$(date +%Y%m%d%H%M%S)"

# Değiştir
sed -i 's/timeout=300/timeout=600/g' "$TARGET"

echo "=== Sonra ==="
grep -n "timeout=600" "$TARGET"
echo "Done: subprocess timeout 300 -> 600"
