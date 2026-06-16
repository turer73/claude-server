#!/bin/bash
# Haftalık Plausible Analytics denetimi — scripts/seo-plausible.py sarmalayıcısı (klipper-cron-wrap.sh
# ile çağrılır → OUTCOME marker'ı cron_outcomes'a düşer). Gerçek-trafik verisi (ziyaretçi/bounce/
# süre/kaynak) → davranış bulguları ortak-hafızaya (type=bug → SessionStart), Telegram yok.
#
# seo-gsc (arama-talep) ile birlikte çalışır: GSC'nin yüksek-gösterim-düşük-CTR bulgusu, Plausible'ın
# bounce/etkileşim verisiyle çapraz-okunduğunda fix'in snippet mi yoksa landing sorunu mu olduğunu
# netleştirir. PLAUSIBLE_3DLABX_KEY ölürse "OUTCOME: fail" → klipper-cron-wrap → alert (sessiz kayıp yok).
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/seo-plausible.py
