#!/usr/bin/env bash
# codex-feed-poll.sh — açık PR'lardaki Codex bulgularını ajan-feed için cache'le.
#
# NEDEN: Codex bulguları yalnız GitHub'da (yerel iz yok) → klipper elle-poll ediyordu.
# agent-feed.sh local-only + hızlı kalmalı (session-start) → ağ-çağrısını BURADA (cron */30) yapıp
# özeti data/hook-state/codex-open.txt'e yazarız; feed o dosyayı offline okur.
#
# Salt-okunur (yalnız cache dosyası yazılır). FAIL-SAFE: gh yok/ağ-yok → eski cache korunur, OUTCOME partial.
# Çıktı satır formatı (feed grep -v '^#' ile okur):  🤖 Codex: PR#176 "başlık" — 2 açık (1 P1, 1 P2)
set -uo pipefail
cd /opt/linux-ai-server || { echo "OUTCOME: fail | cd"; exit 0; }

REPO="${CODEX_FEED_REPO:-turer73/claude-server}"
OUT="${CODEX_FEED_OUT:-data/hook-state/codex-open.txt}"
TMP="${OUT}.tmp"
mkdir -p "$(dirname "$OUT")"

command -v gh >/dev/null 2>&1 || { echo "OUTCOME: partial | gh yok (eski cache korundu)"; exit 0; }

# Açık PR'lar (numara + başlık). Ağ-hatasında eski cache'i koru.
PRS=$(gh pr list --repo "$REPO" --state open --json number,title --jq '.[] | "\(.number)\t\(.title)"' 2>/dev/null) \
    || { echo "OUTCOME: partial | pr-list fetch-fail (eski cache korundu)"; exit 0; }

{
    echo "# codex-open.txt — codex-feed-poll.sh ($(date '+%Y-%m-%d %H:%M')); açık-PR Codex bulgu özeti"
} > "$TMP"

TOTAL_PR=0
FLAGGED=0
while IFS=$'\t' read -r num title; do
    [ -z "$num" ] && continue
    TOTAL_PR=$((TOTAL_PR + 1))
    # Bu PR'ın Codex inline yorumları (P1/P2 badge'li olanları say). Hata → bu PR'ı atla.
    bodies=$(gh api "repos/${REPO}/pulls/${num}/comments" \
        --jq '.[] | select(.user.login|test("codex";"i")) | .body' 2>/dev/null) || continue
    [ -z "$bodies" ] && continue
    p1=$(printf '%s\n' "$bodies" | grep -c 'P1' || true)
    p2=$(printf '%s\n' "$bodies" | grep -c 'P2' || true)
    tot=$(( ${p1:-0} + ${p2:-0} ))
    [ "$tot" -eq 0 ] && continue
    FLAGGED=$((FLAGGED + 1))
    short=$(printf '%s' "$title" | cut -c1-38)
    # NOT: tot = PR'daki TÜM Codex P1/P2 yorumu (çözülen dahil; unresolved-filtresi GraphQL ister, v1'de yok).
    # "açık" demiyoruz → DOĞRULA ile gerçek-durumu PR'da teyit ettiririz.
    echo "🤖 Codex: PR#${num} \"${short}\" — ${tot} bulgu (${p1} P1, ${p2} P2), DOĞRULA" >> "$TMP"
done <<< "$PRS"

mv "$TMP" "$OUT" 2>/dev/null || { echo "OUTCOME: partial | cache yazılamadı"; exit 0; }
echo "OUTCOME: pass | ${TOTAL_PR} açık PR, ${FLAGGED} Codex-bulgulu → cache güncel"
exit 0
