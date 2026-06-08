#!/bin/bash
# RAG haftalık reindex (P0-b, surer): klipper-memory koleksiyonunu güncel DB'den yeniden kur.
# Index manuel-bağımlıydı → 13-May'dan donmuş, 878 oturum+239 memory eksikti. Bu cron tazeler.
# rag_index_all.py sonda 'ALL_INDEX_OK' basar; ona göre OUTCOME emit (kör-başarı değil).
set -uo pipefail
ROOT="/opt/linux-ai-server"
. "$ROOT/scripts/lib/outcome.sh"
OUT="$($ROOT/venv/bin/python3 $ROOT/scripts/rag_index_all.py 2>&1)"
rc=$?
pts="$(printf '%s' "$OUT" | grep -oE 'Final: [0-9]+ points' | grep -oE '[0-9]+' | head -1)"
if [ $rc -eq 0 ] && printf '%s' "$OUT" | grep -q 'ALL_INDEX_OK'; then
    emit_outcome pass "rag-reindex: klipper-memory ${pts:-?} point güncellendi"
else
    emit_outcome fail "rag-reindex BAŞARISIZ (rc=$rc, ALL_INDEX_OK yok): $(printf '%s' "$OUT" | tail -1)"
fi
exit 0
