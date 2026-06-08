#!/bin/bash
# Memory synthesis (LIVESYS-MEMSYN) — haftalık hafıza sentezi sarmalayıcısı.
# DRY_RUN default (yazma YOK); gerçek arşivleme için MEMSYN_APPLY=1 gerekir (+otomatik DB-backup).
# klipper-cron-wrap.sh ile çağrılır → python'un OUTCOME marker'ı cron_outcomes'a düşer.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/memory-synthesize.py
