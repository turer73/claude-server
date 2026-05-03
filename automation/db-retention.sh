#!/bin/bash
# DB retention — keep server.db and ci_tests.db from growing forever.
#
# Policy (configurable via env):
#   METRICS_KEEP_DAYS   = 30   (metrics_history — high-volume, baseline only)
#   ALERTS_KEEP_DAYS    = 30   (alerts — only resolved ones are deleted)
#   AUDIT_KEEP_DAYS     = 90   (audit_log — security trail, longer retention)
#   CI_KEEP_DAYS        = 90   (ci_test_results + ci_failures + ci_runs)
#
# Pass DRY_RUN=1 to count without deleting.

set -euo pipefail

SERVER_DB=${SERVER_DB:-/opt/linux-ai-server/data/server.db}
CI_DB=${CI_DB:-/opt/linux-ai-server/data/ci_tests.db}
LOG_DIR=${LOG_DIR:-/var/log/linux-ai-server}
LOG_FILE=$LOG_DIR/db-retention.log

METRICS_KEEP_DAYS=${METRICS_KEEP_DAYS:-30}
ALERTS_KEEP_DAYS=${ALERTS_KEEP_DAYS:-30}
AUDIT_KEEP_DAYS=${AUDIT_KEEP_DAYS:-90}
CI_KEEP_DAYS=${CI_KEEP_DAYS:-90}

DRY_RUN=${DRY_RUN:-0}

mkdir -p "$LOG_DIR"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log()  { echo "[$TS] $*" | tee -a "$LOG_FILE"; }
sqlite_exec() {
    # On dry-run, replace DELETE with SELECT COUNT(*)
    local db=$1; local stmt=$2
    if [ "$DRY_RUN" = "1" ]; then
        local count_stmt
        count_stmt=$(echo "$stmt" | sed -E 's/^DELETE FROM /SELECT COUNT(*) FROM /')
        sqlite3 "$db" "$count_stmt"
    else
        sqlite3 "$db" "$stmt; SELECT changes();"
    fi
}

if [ "$DRY_RUN" = "1" ]; then
    log "DRY RUN — no rows will be deleted"
fi

if [ ! -f "$SERVER_DB" ]; then
    log "WARN: $SERVER_DB not found, skipping server retention"
else
    log "server.db retention starting"

    # metrics_history — anything older than METRICS_KEEP_DAYS
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM metrics_history WHERE timestamp < datetime('now', '-${METRICS_KEEP_DAYS} days')")
    log "  metrics_history pruned: $n rows (keep ${METRICS_KEEP_DAYS}d)"

    # alerts — only resolved alerts older than ALERTS_KEEP_DAYS;
    # unresolved stay forever (still actionable)
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM alerts WHERE resolved=1 AND timestamp < datetime('now', '-${ALERTS_KEEP_DAYS} days')")
    log "  alerts (resolved) pruned: $n rows (keep ${ALERTS_KEEP_DAYS}d)"

    # audit_log — older than AUDIT_KEEP_DAYS
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM audit_log WHERE timestamp < datetime('now', '-${AUDIT_KEEP_DAYS} days')")
    log "  audit_log pruned: $n rows (keep ${AUDIT_KEEP_DAYS}d)"
fi

if [ ! -f "$CI_DB" ]; then
    log "WARN: $CI_DB not found, skipping ci retention"
else
    log "ci_tests.db retention starting"

    # ci_test_results / ci_failures / ci_project_results all FK to ci_runs.
    # Delete old runs and rely on ON DELETE CASCADE.
    n=$(sqlite_exec "$CI_DB" "DELETE FROM ci_runs WHERE started_at < datetime('now', '-${CI_KEEP_DAYS} days')")
    log "  ci_runs pruned: $n rows (cascades to results/failures, keep ${CI_KEEP_DAYS}d)"
fi

if [ "$DRY_RUN" = "1" ]; then
    log "DRY RUN complete — exiting before VACUUM"
    exit 0
fi

# Reclaim disk space. Order matters in WAL mode:
#   1. checkpoint(TRUNCATE) first  → flush any pending writes into the main DB
#   2. VACUUM                       → rewrite the main DB compacted (uses WAL)
#   3. checkpoint(TRUNCATE) again   → drain VACUUM's WAL back to the DB so the
#                                     -wal sidecar shrinks to 0 bytes
# Without step 3 the WAL grows to ~DB-size during VACUUM and stays that way.
for db in "$SERVER_DB" "$CI_DB"; do
    [ -f "$db" ] || continue
    pre_db=$(stat -c%s "$db" 2>/dev/null || echo 0)
    pre_wal=$(stat -c%s "${db}-wal" 2>/dev/null || echo 0)
    sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE); VACUUM; PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
    post_db=$(stat -c%s "$db" 2>/dev/null || echo 0)
    post_wal=$(stat -c%s "${db}-wal" 2>/dev/null || echo 0)
    saved=$(( (pre_db + pre_wal - post_db - post_wal) / 1024 ))
    log "  VACUUM $(basename "$db"): db ${pre_db}→${post_db}, wal ${pre_wal}→${post_wal} (${saved} KB freed)"
done

log "retention complete"
