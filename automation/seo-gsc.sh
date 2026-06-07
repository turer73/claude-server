#!/bin/bash
# Haftalık Google Search Console denetimi — scripts/seo-gsc.py sarmalayıcısı (klipper-cron-wrap.sh
# ile çağrılır → OUTCOME marker'ı cron_outcomes'a düşer). Gerçek arama verisi (sorgu/CTR/sitemap/
# index) → bulgular ortak-hafızaya (type=bug → SessionStart), Telegram yok.
#
# Bu haftalık çağrı AYNI ZAMANDA OAuth refresh_token'ı sıcak tutar (6-ay-kullanılmazsa-revoke'u
# önler) ve token ölürse "OUTCOME: fail" → klipper-cron-wrap → alert (sessiz kayıp yok). Yani
# ayrı bir liveness-check'e gerek kalmaz — denetim kendi kimlik-başarısızlığını yüzeye çıkarır.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/seo-gsc.py
