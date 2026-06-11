#!/bin/bash
# RAG haftalık reindex (P0-b, surer): klipper-memory koleksiyonunu güncel DB'den yeniden kur.
# Index manuel-bağımlıydı → 13-May'dan donmuş, 878 oturum+239 memory eksikti. Bu cron tazeler.
# rag_index_all.py sonda 'ALL_INDEX_OK' basar; ona göre OUTCOME emit (kör-başarı değil).
set -uo pipefail
ROOT="/opt/linux-ai-server"
. "$ROOT/scripts/lib/outcome.sh"

# Concurrency-kilit (Codex P2 / #566): rag_index_all.py cleanup'i klipper-memory-* (NEW
# haric) HEPSINI siler. Iki reindex cakisirsa Run-A cleanup'i Run-B'nin in-flight/canli
# koleksiyonunu silip alias'i kirar. flock -n: cakisan kosu CALISTIRMA, partial-skip.
LOCK="$ROOT/data/hook-state/rag-reindex.lock"
exec 9>"$LOCK" || { emit_outcome fail "rag-reindex: lock dosyasi acilamadi ($LOCK)"; exit 0; }
if ! flock -n 9; then
    emit_outcome partial "rag-reindex: baska reindex zaten calisiyor (flock), bu kosu atlandi"
    exit 0
fi

OUT="$($ROOT/venv/bin/python3 $ROOT/scripts/rag_index_all.py 2>&1)"
rc=$?
pts="$(printf '%s' "$OUT" | grep -oE 'Final: [0-9]+ points' | grep -oE '[0-9]+' | head -1)"
if [ $rc -eq 0 ] && printf '%s' "$OUT" | grep -q 'ALL_INDEX_OK'; then
    emit_outcome pass "rag-reindex: klipper-memory ${pts:-?} point güncellendi"
else
    emit_outcome fail "rag-reindex BAŞARISIZ (rc=$rc, ALL_INDEX_OK yok): $(printf '%s' "$OUT" | tail -1)"
fi
exit 0
