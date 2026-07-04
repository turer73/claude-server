#!/bin/bash
# gate-promote.sh — G6 insan-tetikli TERFİ (non_required → required).
# Tasarım §4: G6-eval ÖNERİR, İNSAN (Turgut) AKTÜE eder. Bu script branch-protection'ı
# DEĞİŞTİRİR — yalnız --i-am-turgut flag'iyle.
#
# Kullanım: gate-promote.sh <gate_id> --i-am-turgut   (gate_id: g1-repro | g4-invariant)

set -euo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SELF_DIR/_gate-ladder-lib.sh"

GATE_ID="${1:-}"
[ -z "$GATE_ID" ] && { echo "kullanım: $0 <gate_id> --i-am-turgut" >&2; exit 1; }
require_human_flag "$@" || exit 1

CTX="$(gate_context "$GATE_ID")"
[ -z "$CTX" ] && { echo "HATA: bilinmeyen gate_id '$GATE_ID' (context-eşlemesi yok)" >&2; exit 1; }

CHANGES=$(_ladder_set_rung "$GATE_ID" "required" "promote")
[ "$CHANGES" = "0" ] && { echo "HATA: gate_ladder'da kayıt yok: '$GATE_ID' (önce migrate/eval)" >&2; exit 1; }

_protection_update_context "$CTX" add
echo "TERFİ: $GATE_ID → required (branch-protection context '+$CTX'; rung güncellendi)"
