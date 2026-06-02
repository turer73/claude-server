#!/bin/bash
# Günlük operasyon dijesti -> Telegram push (F-C1 / LIVESYS Faz 3.1).
# NOTHING_NEW guard digest.py içinde: sinyal yoksa exit 0, Telegram'a basmaz.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 automation/digest.py --send
