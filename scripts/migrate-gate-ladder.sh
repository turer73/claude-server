#!/bin/bash
# migrate-gate-ladder.sh — G6 gate_ladder state-tablosu bootstrap (idempotent).
# Tasarım: docs/g6-enforcement-ladder-design.md §4/§6. Ev: coverage.db (gate_telemetry ile aynı).
#
# Basamaklar: shadow → non_required → required → demoted → off (§2).
# 2-gate non_required-seed (g1-repro + g4-invariant; ikisi de CANLI-non_required).

set -euo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"
mkdir -p "$(dirname "$DB")"

sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS gate_ladder (
    gate_id      TEXT PRIMARY KEY,
    rung         TEXT NOT NULL DEFAULT 'non_required'
                 CHECK (rung IN ('shadow','non_required','required','demoted','off')),
    since_ts     TEXT NOT NULL DEFAULT (datetime('now')),
    last_eval    TEXT,
    history_json TEXT NOT NULL DEFAULT '[]'
);
-- 2-gate seed (idempotent — çift-koşumda değişmez, mevcut rung KORUNUR).
INSERT OR IGNORE INTO gate_ladder (gate_id, rung) VALUES ('g1-repro', 'non_required');
INSERT OR IGNORE INTO gate_ladder (gate_id, rung) VALUES ('g4-invariant', 'non_required');
SQL

echo "gate_ladder hazır: $DB"
