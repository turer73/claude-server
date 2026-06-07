#!/bin/bash
# Haftalık teknik-SEO denetimi — scripts/seo-audit.py sarmalayıcısı (klipper-cron-wrap.sh
# ile çağrılır → OUTCOME marker'ı cron_outcomes'a düşer). Deterministik, salt-okunur HTTP GET;
# bulgular ortak-hafızaya (type=bug → SessionStart), Telegram yok. Default flagship domainler.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/seo-audit.py
