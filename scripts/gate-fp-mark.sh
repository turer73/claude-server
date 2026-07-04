#!/bin/bash
# gate-fp-mark.sh — G2 human ground-truth: gate-firing'i true_catch/false_positive işaretle.
# Tasarım: docs/g2-gate-telemetry-design.md §2c. Heuristik yalnız-ADAY üretir; KESİN hüküm insandan.
#
# Kullanım: gate-fp-mark.sh <gate_id> <run_id> <true_catch|false_positive> "<gerekçe>"
# (Tasarım-imzasına gate_id eklendi: G2b'de çok-gate — aynı run'da hem g1-repro hem
#  g4-invariant firing olabilir, run_id tek-başına belirsiz. #100388-lane sapma-notu.)

set -euo pipefail

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"

if [ $# -lt 4 ]; then
    echo "kullanım: $0 <gate_id> <run_id> <true_catch|false_positive> \"<gerekçe>\"" >&2
    exit 1
fi
GATE_ID="$1"; RUN_ID="$2"; FP_CLASS="$3"; NOTE="$4"

case "$FP_CLASS" in
    true_catch|false_positive) : ;;
    *) echo "HATA: fp_class 'true_catch' veya 'false_positive' olmalı (verilen: $FP_CLASS)" >&2; exit 1 ;;
esac
case "$RUN_ID" in
    ''|*[!0-9]*) echo "HATA: run_id sayısal olmalı (verilen: $RUN_ID)" >&2; exit 1 ;;
esac

# Fail-loud: kayıt yoksa sessiz-noop DEĞİL (yanlış run_id/gate_id yazımı fark edilsin).
CHANGES=$(sqlite3 "$DB" "
    UPDATE gate_telemetry
       SET fp_class='$FP_CLASS', fp_source='human', note='$(printf '%s' "$NOTE" | tr -d "'")'
     WHERE gate_id='$(printf '%s' "$GATE_ID" | tr -d "'")' AND run_id=$RUN_ID;
    SELECT changes();")
if [ "$CHANGES" = "0" ]; then
    echo "HATA: kayıt yok: gate_id=$GATE_ID run_id=$RUN_ID (önce collector toplamış olmalı)" >&2
    exit 1
fi
echo "işaretlendi: gate=$GATE_ID run=$RUN_ID → $FP_CLASS (human)"
