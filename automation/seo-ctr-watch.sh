#!/bin/bash
# Haftalık SEO CTR-watch — scripts/seo-ctr-watch.py sarmalayıcısı (klipper-cron-wrap.sh ile çağrılır
# → OUTCOME marker'ı cron_outcomes'a düşer). PR#223 'arena yks' title-fix etkisini izler: merge'e
# kadar no-op, merge sonrası +1/+2/+4 hafta GSC CTR/pos'u baseline ile karşılaştırıp ortak-hafıza
# NOT'una yazar (SessionStart-görünür), +4 hafta sonra kendini emekliye ayırır. Telegram yok.
# Plan #654. seo-gsc'den sonra (token sıcak) çalışır.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/seo-ctr-watch.py
