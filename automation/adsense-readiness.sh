#!/bin/bash
# Haftalık AdSense hazırlık denetçisi — scripts/adsense-readiness.py sarmalayıcısı
# (klipper-cron-wrap.sh ile çağrılır → OUTCOME marker'ı cron_outcomes'a düşer).
# Her AdSense sitesi için durum (API) + içerik denetimi → hazırlık-checklist + öneri →
# ortak-hafıza (type=learning), durum-değişimi → type=bug (SessionStart). Salt-okunur, mail yok.
#
# ad-advisor'dan (08:45) sonra (09:00): GSC/AdSense OAuth tazelenmiş olur. Token ölürse
# adsense-readiness.py "OUTCOME: fail" → wrap → alert (sessiz kayıp yok).
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/adsense-readiness.py
