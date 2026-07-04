#!/bin/bash
# gate-telemetry-collect.sh — G2 collector: CI gate-firing'lerini coverage.db'ye topla.
# Tasarım: docs/g2-gate-telemetry-design.md §2b. Desen: test-runner.sh (decoupled cron).
#
# Neden collector, webhook değil: CI→DB kuplajı yok; re-run idempotent (UNIQUE upsert);
# mevcut cron-ritmine (30dk) oturur. Pilot: SADECE g1-repro (repro-gate job'u) —
# G4-job'u eklenince GATE_JOBS listesi genişler (gate_id-parametrik).
#
# Watermark: son-İŞLENEN max run_id — yalnız TAMAMLANMIŞ run'larda ilerler
# (in-progress conclusion='' → SKIP, watermark ilerlemez → sonraki tick toplar; #100388).
#
# Env: COVERAGE_DB, STATE_FILE, REPO, GH_LIMIT (test-edilebilirlik + prod-default).
#
# CRON-KURULUM (klipper-deploy, test-runner ritmi):
#   */30 * * * * /opt/linux-ai-server/automation/gate-telemetry-collect.sh
# BACKFILL (ilk-kurulumda bir-kez, watermark-yokken geçmişi toplar):
#   GH_LIMIT=200 /opt/linux-ai-server/automation/gate-telemetry-collect.sh

set -uo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"
STATE_FILE="${STATE_FILE:-/opt/linux-ai-server/data/hook-state/gate-telemetry-last-run.txt}"
REPO="${REPO:-turer73/claude-server}"
GH_LIMIT="${GH_LIMIT:-40}"
LOG_FILE="${GATE_TELEMETRY_LOG:-/opt/linux-ai-server/data/hook-logs/gate-telemetry.log}"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG_FILE")" 2>/dev/null || true
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

# Tablo yoksa bootstrap (idempotent) — collector tek-başına da ayağa kalkabilir.
COVERAGE_DB="$DB" bash "$(dirname "$0")/../scripts/migrate-gate-telemetry.sh" >/dev/null 2>>"$LOG_FILE" || {
    log "migration FAIL — collector atlanıyor"; exit 2; }

WATERMARK=0
[ -f "$STATE_FILE" ] && WATERMARK=$(tr -dc '0-9' < "$STATE_FILE")
WATERMARK="${WATERMARK:-0}"

# İzlenen gate'ler: "job_adi:gate_id". G2b: g4-invariant eklendi (tasarım 'G4-gelince-genişler').
GATE_JOBS="repro-gate:g1-repro g4-invariant:g4-invariant"

RUNS_JSON=$(gh run list --repo "$REPO" --workflow=CI --limit "$GH_LIMIT" \
    --json databaseId,conclusion,headBranch,headSha,createdAt 2>>"$LOG_FILE") || {
    log "gh run list FAIL"; exit 2; }

PROCESSED=0
NEW_WATERMARK="$WATERMARK"

# id-artan sırada işle (watermark tutarlı ilerlesin)
for RUN_ID in $(printf '%s' "$RUNS_JSON" | jq -r 'sort_by(.databaseId) | .[].databaseId'); do
    [ "$RUN_ID" -le "$WATERMARK" ] && continue
    ROW=$(printf '%s' "$RUNS_JSON" | jq -r --argjson id "$RUN_ID" \
        '.[] | select(.databaseId == $id) | [.conclusion, .headBranch, .headSha, .createdAt] | @tsv')
    IFS=$'\t' read -r CONCLUSION BRANCH SHA CREATED <<< "$ROW"

    # In-progress/queued: conclusion boş → SKIP, watermark İLERLEMEZ (sonraki tick toplar).
    if [ -z "$CONCLUSION" ] || [ "$CONCLUSION" = "null" ]; then
        log "run=$RUN_ID in-progress — bekletiliyor (watermark sabit)"
        break
    fi

    JOBS_JSON=$(gh run view "$RUN_ID" --repo "$REPO" --json jobs 2>>"$LOG_FILE") || {
        log "run=$RUN_ID job-fetch FAIL — bekletiliyor"; break; }

    for PAIR in $GATE_JOBS; do
        JOB_NAME="${PAIR%%:*}"; GATE_ID="${PAIR##*:}"
        JC=$(printf '%s' "$JOBS_JSON" | jq -r --arg n "$JOB_NAME" \
            '.jobs[] | select(.name == $n) | .conclusion // empty' | head -1)
        [ -z "$JC" ] && continue  # bu run'da gate-job yok (push-event CI'ı vb.)

        case "$JC" in
            success)
                VERDICT="pass"
                # N/A-skip ayrımı YALNIZ g1-repro'da (gate-script success-içinde 'atlandı' basar;
                # g4-invariant her-PR'da koşar, skip_na kavramı yok → log-fetch masrafı da yok).
                if [ "$GATE_ID" = "g1-repro" ]; then
                    JOB_ID=$(printf '%s' "$JOBS_JSON" | jq -r --arg n "$JOB_NAME" \
                        '.jobs[] | select(.name == $n) | .databaseId' | head -1)
                    if gh run view --repo "$REPO" --job "$JOB_ID" --log 2>>"$LOG_FILE" | grep -q "repro-gate atland"; then
                        VERDICT="skip_na"
                    fi
                fi ;;
            failure) VERDICT="fail" ;;
            *)       VERDICT="" ;;  # cancelled/skipped-job → kayıt-yok
        esac
        [ -z "$VERDICT" ] && continue

        # branch → PR-numarası (yoksa NULL)
        PR_NUM=$(gh pr list --repo "$REPO" --head "$BRANCH" --state all --limit 1 \
            --json number --jq '.[0].number // empty' 2>>"$LOG_FILE")

        sqlite3 "$DB" "INSERT INTO gate_telemetry (gate_id, run_id, pr_number, head_sha, branch, ts, verdict)
            VALUES ('$GATE_ID', $RUN_ID, $([ -n "$PR_NUM" ] && echo "$PR_NUM" || echo NULL),
                    '$SHA', '$(printf '%s' "$BRANCH" | tr -d "'")', '$CREATED', '$VERDICT')
            ON CONFLICT(gate_id, run_id) DO UPDATE SET
                verdict=excluded.verdict, pr_number=excluded.pr_number, ts=excluded.ts;" 2>>"$LOG_FILE" || {
            log "run=$RUN_ID upsert FAIL"; continue; }
        PROCESSED=$((PROCESSED + 1))
        log "run=$RUN_ID gate=$GATE_ID verdict=$VERDICT pr=${PR_NUM:-—} branch=$BRANCH"
    done
    NEW_WATERMARK="$RUN_ID"
done

[ "$NEW_WATERMARK" -gt "$WATERMARK" ] && printf '%s' "$NEW_WATERMARK" > "$STATE_FILE"
log "tick bitti: processed=$PROCESSED watermark=$NEW_WATERMARK"
exit 0
