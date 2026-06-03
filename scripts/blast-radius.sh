#!/bin/bash
# blast-radius.sh — LIVESYS FAZ 4: değişiklik-öncesi etki haritası (hafif, grep-tabanlı).
#
# Modlar:
#   blast-radius.sh <dosya>          -> TEK dosya (S1): (a) dokunduğu DB tabloları (2-hop)
#                                       (b) reverse-dep consumer'lar.
#   blast-radius.sh --diff [range]   -> CHANGESET (S2): git diff --name-only ile değişen TÜM
#                                       dosyaların AGREGAT etki-haritası. range vars=@{u}...HEAD.
#
# Felsefe: heavyweight AST/import-graph DEĞİL — deterministik grep. read-only.
set -uo pipefail

# ROOT'u script konumundan türet (hardcoded DEĞİL) — clone/checkout yeri fark etmez.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIRS=(app automation scripts)
GREP_X=(--exclude-dir=__pycache__ --exclude=*.pyc -I)
cd "$ROOT" || exit 1

# Bir dosyadaki DB tablo KULLANIMI (INSERT INTO/UPDATE/FROM). Python import satırları
# DIŞLANIR; CREATE TABLE sayılmaz (şema-tanımı). prose/dosya-adı false-positive stopword'le elenir.
_tables_in() {
    # SQL tırnaklı string içinde. INSERT INTO/UPDATE net-SQL (her tırnaklı satırda).
    # FROM zayıf-sinyal (docstring "from tracking" de tırnaklı) -> YALNIZ SELECT-içeren
    # satırda say (gerçek sorgu). Böylece docstring/prose "from X" elenir, "SELECT..FROM t" kalır.
    local q
    q="$(grep -hvE "^[[:space:]]*(from|import)[[:space:]]" "$1" 2>/dev/null | grep -hE "[\"']")"
    {
        printf '%s\n' "$q" | grep -hoiE "(INSERT INTO|UPDATE)[[:space:]]+[a-z_][a-z0-9_]*"
        printf '%s\n' "$q" | grep -hiE "select" | grep -hoiE "FROM[[:space:]]+[a-z_][a-z0-9_]*"
    } |
        awk '{print tolower($NF)}' |
        grep -ivE "^(select|where|order|group|limit|set|by|as|on|values|null|server|coverage|claude_memory|rag_metrics)$" |
        sort -u
}
_local_imports() {
    grep -hoE "from (app\.(core|db)\.[a-z_]+) import|import (app\.(core|db)\.[a-z_]+)" "$1" 2>/dev/null |
        grep -oE "app\.(core|db)\.[a-z_]+" | sort -u
}
_mod_to_path() { echo "${1//.//}.py"; }

# 2-hop forward tablolar: dosya + import ettiği app.core/db modülleri (database.py şema-home atla).
_forward_tables() {
    local abs="$1" m mp
    _tables_in "$abs"
    while IFS= read -r m; do
        [ -n "$m" ] || continue
        [ "$m" = "app.db.database" ] && continue
        mp="$(_mod_to_path "$m")"
        [ -f "$mp" ] && _tables_in "$mp"
    done < <(_local_imports "$abs")
}

# ── Değişen dosya listesini topla (mod'a göre) ──
declare -a CHANGED=()
if [ "${1:-}" = "--diff" ]; then
    RANGE="${2:-}"
    if [ -z "$RANGE" ]; then
        RANGE="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1 && echo '@{upstream}...HEAD' || echo 'HEAD~1')"
    fi
    HEADER="CHANGESET ($RANGE)"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in app/*.py | automation/*.sh | scripts/*.sh | app/*/*.py | app/*/*/*.py) ;; *) continue ;; esac
        [ -f "$line" ] && CHANGED+=("$line")
    done < <(git diff --name-only "$RANGE" 2>/dev/null)
    if [ "${#CHANGED[@]}" -eq 0 ]; then
        echo "blast-radius --diff: '$RANGE' içinde taranabilir (py/sh) değişen dosya yok." >&2
        exit 0
    fi
else
    f="${1:-}"
    [ -z "$f" ] && { echo "kullanim: blast-radius.sh <dosya> | --diff [range]" >&2; exit 1; }
    rel="${f#"$ROOT"/}"
    [ -f "$ROOT/$rel" ] || { echo "blast-radius: dosya yok: $f" >&2; exit 1; }
    HEADER="$rel"
    CHANGED=("$rel")
fi

# ── Agregat forward tablolar + değişen-set (exclude için) ──
declare -A TBL
EXCL=""
for rel in "${CHANGED[@]}"; do
    EXCL="${EXCL}${rel}"$'\n'
    while IFS= read -r t; do [ -n "$t" ] && TBL["$t"]=1; done < <(_forward_tables "$ROOT/$rel")
done

# değişen-set'i grep-dışla (consumer'lar değişikliğin KENDİSİ değil)
_not_changed() { grep -vxF -f <(printf '%s' "$EXCL") 2>/dev/null || cat; }

echo "== BLAST-RADIUS: $HEADER =="
if [ "${#CHANGED[@]}" -gt 1 ]; then
    echo "-- değişen dosyalar (${#CHANGED[@]}):"
    printf '   - %s\n' "${CHANGED[@]}"
fi

echo "-- dokunulan DB tablolari (2-hop):"
if [ "${#TBL[@]}" -eq 0 ]; then
    echo "   (yok — DB'ye dokunmuyor)"
else
    for t in $(printf '%s\n' "${!TBL[@]}" | sort); do echo "   - $t"; done
fi

echo "-- consumer'lar (reverse-dep; değişen-set hariç):"
# (a) değişen .py modüllerini import edenler
for rel in "${CHANGED[@]}"; do
    [[ "$rel" == *.py ]] || continue
    modname="${rel%.py}"; modname="${modname//\//.}"
    imp="$(grep -rlE "(from|import)[[:space:]]+${modname//./\\.}([[:space:]]|\.|$)" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | _not_changed | sort -u)"
    [ -n "$imp" ] && { echo "   [${rel} import eden]:"; echo "$imp" | sed 's/^/     - /'; }
done
# (b) dokunulan tabloları okuyan/yazanlar (+ events dolaylı emit çağıranlar)
for t in $(printf '%s\n' "${!TBL[@]}" | sort); do
    users="$(grep -rliE "(INTO|FROM|UPDATE|TABLE)[[:space:]]+${t}([[:space:]]|\(|;|,|$)" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | _not_changed | sort -u)"
    if [ "$t" = "events" ]; then
        indirect="$(grep -rlE "emit-event\.sh|emit_event\(" "${GREP_X[@]}" "${SCAN_DIRS[@]}" 2>/dev/null | _not_changed | sort -u)"
        users="$(printf '%s\n%s\n' "$users" "$indirect" | grep -v '^$' | sort -u)"
    fi
    [ -n "$users" ] && { echo "   [tablo '$t' okuyan/yazan]:"; echo "$users" | sed 's/^/     - /'; }
done

exit 0
