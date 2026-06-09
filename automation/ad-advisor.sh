#!/bin/bash
# Haftalık reklam-başlatma danışmanı — scripts/ad-advisor.py sarmalayıcısı (klipper-cron-wrap.sh
# ile çağrılır → OUTCOME marker'ı cron_outcomes'a düşer). GSC arama verisinden yüksek-talep/
# düşük-CTR (reklam-değer) kelimeleri tespit eder + /claude ile reklam-metni taslağı üretir →
# bulgular ortak-hafızaya (type=learning → SessionStart), Telegram/mail yok. Salt-okunur.
#
# seo-gsc'den HEMEN SONRA (Pzt 08:45) koşar: o ana kadar seo-gsc OAuth token'ı tazelenmiş +
# GSC verisi güncel olur. Token ölürse ad-advisor.py "OUTCOME: fail" emit eder → wrap → alert.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/ad-advisor.py
