#!/bin/bash
# Claim-gate poller sarmalayıcısı (paket 3/3) — klipper-cron-wrap.sh ile çağrılır,
# python'un OUTCOME marker'ı cron_outcomes'a düşer. DEFAULT advisory (CLAIM_GATE_ENFORCE=0).
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 automation/claim-gate-poller.py
