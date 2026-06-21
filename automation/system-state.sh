#!/usr/bin/env bash
# system-state.sh — LSA Faz-2 cron wrapper: günlük "Sistem Durumu" longitudinal sentezi.
# system-state.py salt-okunur → discovery(learning, skip_dedup, tarih-unique). OUTCOME marker cron-wrap.
exec /opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/system-state.py
