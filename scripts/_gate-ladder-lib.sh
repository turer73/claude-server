# _gate-ladder-lib.sh — G6 aktüasyon ortak-mantığı (source-edilir, tek-başına çalışmaz).
# Tasarım: docs/g6-enforcement-ladder-design.md §4. İNSAN-AKTÜASYON: gate-promote/demote
# bunu source eder. G6-eval ÖNERİR; bu helper'lar Turgut-onayıyla branch-protection'ı DEĞİŞTİRİR.
#
# gate_id → CI-job-context eşlemesi (branch-protection required_status_checks.contexts job-adı ister).

DB="${COVERAGE_DB:-/opt/linux-ai-server/data/coverage.db}"
REPO="${REPO:-turer73/claude-server}"
BRANCH="${PROTECTED_BRANCH:-master}"

# gate_id → job-context (2-gate pilot; yeni-gate eklenince buraya).
gate_context() {
    case "$1" in
        g1-repro)     echo "repro-gate" ;;
        g4-invariant) echo "g4-invariant" ;;
        *) echo "" ;;
    esac
}

# İnsan-onay-flag guard (over-reach-guard, §4). --i-am-turgut ZORUNLU.
require_human_flag() {
    local found=0
    for a in "$@"; do [ "$a" = "--i-am-turgut" ] && found=1; done
    if [ "$found" != "1" ]; then
        echo "REDDEDİLDİ: aktüasyon insan-onayı gerektirir. '--i-am-turgut' flag'i zorunlu (G6 §4)." >&2
        return 1
    fi
}

# gate_ladder rung + history güncelle (denetim-izi). changes() döner (0 = kayıt-yok).
_ladder_set_rung() {
    local gate_id="$1" rung="$2" action="$3"
    sqlite3 "$DB" "
        UPDATE gate_ladder
           SET rung='$rung', since_ts=datetime('now'),
               history_json=json_insert(
                   COALESCE(history_json,'[]'), '\$[#]',
                   json_object('action','$action','rung','$rung','by','human'))
         WHERE gate_id='$(printf '%s' "$gate_id" | tr -d "'")';
        SELECT changes();"
}

# branch-protection required-check contexts'ini oku → modify → yaz (idempotent).
# op: 'add' | 'remove'. gh-api gerektirir (fake-gh ile test-edilebilir).
_protection_update_context() {
    local ctx="$1" op="$2"
    local current
    current=$(gh api "repos/$REPO/branches/$BRANCH/protection/required_status_checks/contexts" 2>/dev/null || echo '[]')
    local newlist
    if [ "$op" = "add" ]; then
        newlist=$(printf '%s' "$current" | jq -c --arg c "$ctx" '. + [$c] | unique')
    else
        newlist=$(printf '%s' "$current" | jq -c --arg c "$ctx" 'map(select(. != $c))')
    fi
    printf '%s' "$newlist" | gh api -X PUT "repos/$REPO/branches/$BRANCH/protection/required_status_checks/contexts" --input - >/dev/null
}
