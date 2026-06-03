#!/bin/bash
# emit-event.sh — LIVESYS Faz 3.2: TEK bash emit-helper (server.db events tablosu).
#
# Bash uretici-noktalari (cron-wrap, liveness, ...) sqlite3-INSERT'leri DAGITMASIN;
# hepsi bu helper'i cagirir (Python uretici-noktalari app.core.events.emit_event).
# Boylece severity-normalize + SQL-escape + DB-path tek yerde, drift yok.
#
# Kullanim: emit-event.sh <type> <source> <severity> <title> [detail]
# Garanti: FAIL-SAFE (exit 0 — caginan uretici-job'u ASLA dusurme; modul best-effort
#          sozlesmesinin bash karsiligi), SQL-escape, DB-yoksa-sessiz-skip.
#
# NOT: notified=0 (schema default). Bu helper YALNIZCA kayit yazar; bildirim AYRI
# notify-cron'un isi (henuz yok -> mevcut alert-POST tek-notifier kalir, cift-bildirim yok).
set +e

TYPE="${1:-}"
SOURCE="${2:-}"
SEV_IN="${3:-info}"
TITLE="${4:-}"
DETAIL="${5:-}"

# Eksik zorunlu alan -> sessiz no-op (Python emit_event'in 'eksik->None' karsiligi).
if [ -z "$TYPE" ] || [ -z "$SOURCE" ] || [ -z "$TITLE" ]; then
    exit 0
fi

DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
[ -f "$DB_PATH" ] || exit 0

# severity normalize — app/core/events.py _normalize_severity ile BIREBIR tutarli
# (warning->warn, error->critical; bilinmeyen->info). Mevcut alert vocabulary'si
# "warning"/"critical" kullaniyor; bunlar dogrudan notifyable kanonik degere insin.
SEV="$(printf '%s' "$SEV_IN" | tr '[:upper:]' '[:lower:]')"
case "$SEV" in
    warning) SEV="warn" ;;
    error | err | crit) SEV="critical" ;;
    info | warn | critical) ;;
    *) SEV="info" ;;
esac

# SQL-escape (cron_outcomes deseni): backslash/backtick/quote sil, whitespace tek-bosluk,
# kirp, sonra single-quote ikile.
esc() {
    local s
    s="$(printf '%s' "$1" | tr -d '\\`"' | tr '\n\r\t' '   ' | head -c 300)"
    printf '%s' "${s//\'/\'\'}"
}
S_TYPE="$(esc "$TYPE")"
S_SRC="$(esc "$SOURCE")"
S_TITLE="$(esc "$TITLE")"
S_DET="$(esc "$DETAIL")"

sqlite3 "$DB_PATH" \
    "INSERT INTO events (type,source,severity,title,detail) VALUES ('${S_TYPE}','${S_SRC}','${SEV}','${S_TITLE}','${S_DET}');" \
    2>/dev/null || true

exit 0
