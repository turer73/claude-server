#!/bin/bash
# gate-demote.sh — G6 insan-tetikli DÜŞÜRME (required → non_required).
# Tasarım §4: G6-eval drift-ÖNERİR, İNSAN AKTÜE eder. Branch-protection'dan required-check
# çıkarır — yalnız --i-am-turgut flag'iyle. (off DEĞİL: geri-alınabilir; off yalnız-insan-ayrı.)
#
# Kullanım: gate-demote.sh <gate_id> --i-am-turgut

set -euo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SELF_DIR/_gate-ladder-lib.sh"

GATE_ID="${1:-}"
[ -z "$GATE_ID" ] && { echo "kullanım: $0 <gate_id> --i-am-turgut" >&2; exit 1; }
require_human_flag "$@" || exit 1

CTX="$(gate_context "$GATE_ID")"
[ -z "$CTX" ] && { echo "HATA: bilinmeyen gate_id '$GATE_ID' (context-eşlemesi yok)" >&2; exit 1; }

CHANGES=$(_ladder_set_rung "$GATE_ID" "non_required" "demote")
[ "$CHANGES" = "0" ] && { echo "HATA: gate_ladder'da kayıt yok: '$GATE_ID'" >&2; exit 1; }

_protection_update_context "$CTX" remove
echo "DÜŞÜRÜLDÜ: $GATE_ID → non_required (branch-protection context '-$CTX'; rung güncellendi)"
