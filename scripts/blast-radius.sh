#!/bin/bash
# blast-radius.sh — LIVESYS FAZ 4 S1: değişiklik-öncesi etki haritası (hafif, grep-tabanlı).
#
# Bir değişen dosya için: (a) dokunduğu DB tabloları (dosya + import ettiği app.core/app.db
# modülleri = 2-hop) (b) ona/o tablolara bağlı consumer'lar (reverse-dep). PR-review'a beslenir.
#
# Kullanim: blast-radius.sh <dosya-yolu>   (repo köküne göre veya mutlak)
# Felsefe: heavyweight AST/import-graph DEĞİL — deterministik grep, mevcut araç-deseni.
# Garanti: read-only (hiçbir şey değiştirmez), bulunamayan dosya -> uyarı + exit 1.
set -uo pipefail

ROOT="/opt/linux-ai-server"
SCAN_DIRS=(app automation scripts)
GREP_X=(--exclude-dir=__pycache__ --exclude=*.pyc -I)

f="${1:-}"
if [ -z "$f" ]; then
    echo "kullanim: blast-radius.sh <dosya-yolu>" >&2
    exit 1
fi
rel="${f#"$ROOT"/}"
abs="$ROOT/$rel"
if [ ! -f "$abs" ]; then
    echo "blast-radius: dosya yok: $f" >&2
    exit 1
fi

cd "$ROOT" || exit 1

# Bir dosyadaki DB tablo KULLANIMI (INSERT INTO / UPDATE / FROM). Python `from X import`
# / `import X` satırları DIŞLANIR (SQL FROM ile karışmasın); CREATE TABLE sayılmaz
# (şema-tanımı = kullanım değil, yoksa database.py tüm tabloları bulaştırır).
_tables_in() {
    # NOT (grep-limiti): prose "from X" (yorum/string) + DB-dosya-adi (server/coverage/
    # claude_memory.db) yanlis-pozitif uretebilir; SQL stopword + İngilizce-filler eler.
    # Tam ayrim AST gerektirir (S2). Tek-DB kaynak modullerde (orn. events.py) temiz.
    grep -hvE "^[[:space:]]*(from|import)[[:space:]]" "$1" 2>/dev/null |
        grep -hoiE "(INSERT INTO|UPDATE|FROM)[[:space:]]+[a-z_][a-z0-9_]*" |
        awk '{print tolower($NF)}' |
        grep -ivE "^(select|where|order|group|limit|set|by|as|on|values|null|a|the|this|that|here|there|them|it|then|now|above|below|each|both|one|server|coverage|claude_memory|rag_metrics)$" |
        sort -u
}

_local_imports() {
    grep -hoE "from (app\.(core|db)\.[a-z_]+) import|import (app\.(core|db)\.[a-z_]+)" "$1" 2>/dev/null |
        grep -oE "app\.(core|db)\.[a-z_]+" | sort -u
}
_mod_to_path() { echo "${1//.//}.py"; }

echo "== BLAST-RADIUS: $rel =="

# ── (a) Forward: dokunulan tablolar (dosya + 2-hop import'lar; database.py şema-home, atla) ──
declare -A TBL
while IFS= read -r t; do [ -n "$t" ] && TBL["$t"]=1; done < <(_tables_in "$abs")
while IFS= read -r m; do
    [ -n "$m" ] || continue
    [ "$m" = "app.db.database" ] && continue # şema-tanımı home, kullanım değil
    mp="$(_mod_to_path "$m")"
    [ -f "$mp" ] || continue
    while IFS= read -r t; do [ -n "$t" ] && TBL["$t"]=1; done < <(_tables_in "$mp")
done < <(_local_imports "$abs")

echo "-- dokundugu DB tablolari (2-hop: dosya + app.core/db import'lari):"
if [ "${#TBL[@]}" -eq 0 ]; then
    echo "   (yok — DB'ye dokunmuyor)"
else
    for t in $(printf '%s\n' "${!TBL[@]}" | sort); do echo "   - $t"; done
fi

# ── (b) Reverse: bu modülü import eden + bu tabloları okuyan/yazan consumer'lar ──
echo "-- consumer'lar (reverse-dep):"

if [[ "$rel" == *.py ]]; then
    modname="${rel%.py}"
    modname="${modname//\//.}"
    importers="$(grep -rlE "(from|import)[[:space:]]+${modname//./\\.}([[:space:]]|\.|$)" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | grep -v "^${rel}$" | sort -u)"
    [ -n "$importers" ] && { echo "   [import eden]:"; echo "$importers" | sed 's/^/     - /'; }
fi

for t in $(printf '%s\n' "${!TBL[@]}" | sort); do
    users="$(grep -rliE "(INTO|FROM|UPDATE|TABLE)[[:space:]]+${t}([[:space:]]|\(|;|,|$)" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | grep -v "^${rel}$" | sort -u)"
    # 'events' tablosu: dolaylı yazıcılar (emit-event.sh / emit_event() çağıranlar)
    if [ "$t" = "events" ]; then
        indirect="$(grep -rlE "emit-event\.sh|emit_event\(" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | grep -v "^${rel}$" | sort -u)"
        users="$(printf '%s\n%s\n' "$users" "$indirect" | grep -v '^$' | sort -u)"
    fi
    [ -n "$users" ] && { echo "   [tablo '$t' okuyan/yazan]:"; echo "$users" | sed 's/^/     - /'; }
done

exit 0
