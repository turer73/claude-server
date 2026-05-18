#!/bin/bash
# migrate-spawn-failures.sh — P0.2 DLQ table bootstrap (idempotent)
#
# Tablo: spawn_failures (Claude binary spawn fail recovery)
# Lifecycle: pending_retry -> archived (success) | poison (3x fail) | orphaned (note deleted)

set -euo pipefail

DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"

if [ ! -f "$DB" ]; then
    echo "ERROR: DB not found: $DB" >&2
    exit 1
fi

sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS spawn_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    from_device TEXT NOT NULL,
    title TEXT NOT NULL,
    preview TEXT,
    attempt_num INTEGER NOT NULL DEFAULT 1,
    exit_code INTEGER NOT NULL,
    error_log TEXT,
    spawn_log_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending_retry',
    first_failed_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_retry_at TEXT,
    archived_at TEXT,
    poisoned_at TEXT,
    UNIQUE(note_id)
);
CREATE INDEX IF NOT EXISTS idx_spawn_failures_status ON spawn_failures(status, last_retry_at);
CREATE INDEX IF NOT EXISTS idx_spawn_failures_note ON spawn_failures(note_id);
SQL

echo "spawn_failures table ready: $DB"
sqlite3 "$DB" ".schema spawn_failures"
