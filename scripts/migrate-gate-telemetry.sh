#!/bin/bash
# migrate-gate-telemetry.sh — G2 gate_telemetry tablo bootstrap (idempotent).
# Tasarım: docs/g2-gate-telemetry-design.md §2a. Desen: migrate-spawn-failures.sh.
#
# Ev: coverage.db (test_runs ile aynı — CI/gate-trend tek-DB'de).
# UNIQUE(gate_id, run_id): collector re-run'ı idempotent-upsert yapar.

set -euo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"

# coverage.db yoksa oluştur (run-all-tests.sh de ilk-koşumda oluşturur; sıra-bağımsızlık).
mkdir -p "$(dirname "$DB")"

sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS gate_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    pr_number INTEGER,
    head_sha TEXT,
    branch TEXT,
    ts TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','skip_na')),
    fp_class TEXT NOT NULL DEFAULT 'unknown' CHECK (fp_class IN ('unknown','true_catch','false_positive')),
    fp_source TEXT CHECK (fp_source IN ('auto_heuristic','human') OR fp_source IS NULL),
    note TEXT,
    UNIQUE(gate_id, run_id)
);
SQL

echo "gate_telemetry hazır: $DB"
