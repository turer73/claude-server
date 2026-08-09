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
CRON_OUTCOMES_KEEP_DAYS=${CRON_OUTCOMES_KEEP_DAYS:-90}  # LIVESYS Faz1 cron_outcomes
EVENTS_KEEP_DAYS=${EVENTS_KEEP_DAYS:-60}               # audit P1#8 events-spine
REMEDIATION_KEEP_DAYS=${REMEDIATION_KEEP_DAYS:-90}     # audit P1#8 FAZ5 ledger

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
        sqlite3 -cmd ".timeout 10000" "$db" "$count_stmt"
    else
        sqlite3 -cmd ".timeout 10000" "$db" "$stmt; SELECT changes();"
    fi
}

# OUTCOME marker via EXIT trap — covers ALL exit paths (success, dry-run early
# exit, and set -e abort mid-retention). A trailing `echo OUTCOME: pass` only
# fires on a full real run, so dry-run and failures fell through to the wrap.sh
# rc-fallback ("outcome-undefined"). The trap emits an explicit pass/fail every
# time, honoring the LIVESYS Faz1 outcome-contract (rc=0 alone is not success).
_emit_outcome() {
    local rc=$?
    local tag=""
    [ "$DRY_RUN" = "1" ] && tag=" (dry-run)"
    if [ "$rc" -eq 0 ]; then
        echo "OUTCOME: pass | retention complete${tag}"
    else
        echo "OUTCOME: fail | rc=$rc — retention abort${tag} (son log: $LOG_FILE)"
    fi
}
trap _emit_outcome EXIT

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

    # cron_outcomes (LIVESYS Faz1) — older than CRON_OUTCOMES_KEEP_DAYS
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM cron_outcomes WHERE timestamp < datetime('now', '-${CRON_OUTCOMES_KEEP_DAYS} days')")
    log "  cron_outcomes pruned: $n rows (keep ${CRON_OUTCOMES_KEEP_DAYS}d)"

    # events (audit P1#8) — retention yoktu, sınırsız büyürdü (notify-cron/spine ana tablo).
    # datetime() format-agnostik (ISO-T + boşluk) — gün-granül, perf-önemsiz.
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM events WHERE datetime(timestamp) < datetime('now', '-${EVENTS_KEEP_DAYS} days')")
    log "  events pruned: $n rows (keep ${EVENTS_KEEP_DAYS}d)"

    # remediation_log (audit P1#8) — retention yoktu, sınırsız büyürdü (FAZ5 ledger).
    n=$(sqlite_exec "$SERVER_DB" "DELETE FROM remediation_log WHERE datetime(timestamp) < datetime('now', '-${REMEDIATION_KEEP_DAYS} days')")
    log "  remediation_log pruned: $n rows (keep ${REMEDIATION_KEEP_DAYS}d)"
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

# Hook-state TTL — edited-files-*.log per-session log'lari, kullanildiktan sonra
# inceleme degeri yok. 30 gun TTL. VACUUM erken-exit'inden once cunku DB ile ilgisiz.
HOOK_STATE_DIR=${HOOK_STATE_DIR:-/opt/linux-ai-server/data/hook-state}
if [ -d "$HOOK_STATE_DIR" ]; then
    pre_count=$(find "$HOOK_STATE_DIR" -name "edited-files-*.log" -type f 2>/dev/null | wc -l)
    if [ "$DRY_RUN" = "1" ]; then
        purge_count=$(find "$HOOK_STATE_DIR" -name "edited-files-*.log" -type f -mtime +30 2>/dev/null | wc -l)
        log "  [dry-run] hook-state edited-files: ${purge_count}/${pre_count} would be deleted"
    else
        find "$HOOK_STATE_DIR" -name "edited-files-*.log" -type f -mtime +30 -delete 2>/dev/null || true
        post_count=$(find "$HOOK_STATE_DIR" -name "edited-files-*.log" -type f 2>/dev/null | wc -l)
        log "  hook-state edited-files: ${pre_count}→${post_count}"
    fi
fi

# Hook-logs TTL — autonomous-claude-spawn-{noteid}-{ts}.log per-spawn cikti.
# Append-mode top-level log'lar (autonomous-claude.log vb.) ad bazli pattern'a girmez.
HOOK_LOGS_DIR=${HOOK_LOGS_DIR:-/opt/linux-ai-server/data/hook-logs}
if [ -d "$HOOK_LOGS_DIR" ]; then
    for pattern in "autonomous-claude-spawn-*.log" "autonomous-claude-retry-spawn-*.log"; do
        pre_count=$(find "$HOOK_LOGS_DIR" -name "$pattern" -type f 2>/dev/null | wc -l)
        if [ "$DRY_RUN" = "1" ]; then
            purge_count=$(find "$HOOK_LOGS_DIR" -name "$pattern" -type f -mtime +30 2>/dev/null | wc -l)
            log "  [dry-run] hook-logs $pattern: ${purge_count}/${pre_count} would be deleted"
        else
            find "$HOOK_LOGS_DIR" -name "$pattern" -type f -mtime +30 -delete 2>/dev/null || true
            post_count=$(find "$HOOK_LOGS_DIR" -name "$pattern" -type f 2>/dev/null | wc -l)
            log "  hook-logs $pattern: ${pre_count}→${post_count}"
        fi
    done
fi

if [ "$DRY_RUN" = "1" ]; then
    log "DRY RUN complete — exiting before checkpoint"
    exit 0
fi

# VACUUM KALDIRILDI (2026-08-09, kesif 1462 kok-nedeni). ONCEDEN:
#   "PRAGMA wal_checkpoint(TRUNCATE); VACUUM; PRAGMA wal_checkpoint(TRUNCATE);"
# NEDEN KALDIRILDI — VACUUM CANLI DB'yi bozuyordu:
#   VACUUM tum veritabanini (~420MB) bastan yazar, WAL moddayken bu once WAL'e gider.
#   Sondaki checkpoint(TRUNCATE) WAL'i geri bosaltmali AMA 2 uvicorn worker + cron
#   yazicilari aktifken TRUNCATE checkpoint kilit alamaz ve SESSIZCE basarisiz olur.
#   Sonuc: DB hic kuculmuyor, WAL ~DB-boyutuna sisiyor, ve VACUUM'un yarim kalan
#   B-tree/indeks yeniden-insasi bozulma uretiyor:
#     "2nd reference to page X" + "wrong # of entries in index idx_*"
#   Script bunu AYLARDIR log'luyordu ve kimse okumamisti (negatif "freed"):
#     VACUUM server.db: db 453500928->453500928, wal 11383592->443789952 (-422271 KB freed)
#   2026-08-09 (Pazar) canli yakalandi: 02:00 VACUUM -> 13:30 checkpoint -> malformed.
#   Tarihsel uyum: bozulma TESPIT tarihleri onceki Pazar'dan 0/3/2/2/0 gun sonra (5'te 4).
# KAZANC KAYBI YOK: VACUUM zaten hicbir sey kazandirmiyordu (db boyutu degismiyordu).
# Yer geri kazanmak GEREKIRSE: servis DURDURULMUS halde elle VACUUM (tek-yazici sart).
# Checkpoint TEK BASINA guvenli: kilit alamazsa no-op doner, veri yeniden yazilmaz.
for db in "$SERVER_DB" "$CI_DB"; do
    [ -f "$db" ] || continue
    pre_wal=$(stat -c%s "${db}-wal" 2>/dev/null || echo 0)
    sqlite3 -cmd ".timeout 30000" "$db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
    post_wal=$(stat -c%s "${db}-wal" 2>/dev/null || echo 0)
    log "  checkpoint $(basename "$db"): wal ${pre_wal}→${post_wal} (VACUUM yok — kesif 1462)"
done

log "retention complete"
# OUTCOME marker emitted by _emit_outcome EXIT trap (covers success/dry-run/fail).
