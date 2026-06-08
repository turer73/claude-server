#!/bin/bash
# Intent-liveness audit (LIVESYS-INTRO) — salt-okunur öz-introspeksiyon sarmalayıcısı.
# klipper-cron-wrap.sh ile çağrılır → python'un OUTCOME marker'ı cron_outcomes'a düşer.
# Deklarasyon↔gerçek boşluğu (ölü-refleks/orphan-cron) tespit eder; HİÇBİR mutasyon yok.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/intent-liveness-audit.py
