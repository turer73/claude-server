#!/bin/bash
# memory-triage.sh -- Rule-based discovery triage (LLM-free MVP).
# 60+ gun active learning + read_count=0 -> obsolete (low-value learnings auto-archive)
# 60+ gun active workaround -> superseded (workaround eski, fix gelmis olabilir)
# 30+ gun active config -> review needed (config drift kontrol)
#
# Tetikleyici: cron 03:15 (archive-stale 02:30 sonrasi)
# Cikti: log + ozet
set -uo pipefail

DB="/opt/linux-ai-server/data/claude_memory.db"
LOG="/opt/linux-ai-server/data/hook-logs/triage.log"
TS=$(date -Iseconds)

mkdir -p "$(dirname "$LOG")" 2>/dev/null

if [ ! -r "$DB" ]; then
    echo "[$TS] FATAL: DB not readable" >> "$LOG"
    exit 0
fi

# Rule 1: 60+ gun active learning + read_count=0 -> obsolete
R1=$(sqlite3 "$DB" "UPDATE discoveries SET status='obsolete'
                    WHERE status='active' AND type='learning' AND read_count=0
                    AND julianday('now') - julianday(created_at) > 60;
                    SELECT changes();")

# Rule 2: 60+ gun active workaround -> superseded
R2=$(sqlite3 "$DB" "UPDATE discoveries SET status='superseded'
                    WHERE status='active' AND type='workaround'
                    AND julianday('now') - julianday(created_at) > 60;
                    SELECT changes();")

# Rule 3: Counter — 30+ gun config aktif kalanlar (alert, no auto-action)
R3=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries
                    WHERE status='active' AND type='config'
                    AND julianday('now') - julianday(created_at) > 30;")

echo "[$TS] triage: r1=${R1:-0} r2=${R2:-0} r3_alert=${R3:-0}" >> "$LOG"

exit 0
