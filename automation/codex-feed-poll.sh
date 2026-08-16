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
# Repo kokunu SABIT yazma, script'in kendi konumundan turet. Sabit
# `cd /opt/linux-ai-server` uretimde calisiyordu ama CI checkout'u
# /home/runner/work/... altinda oldugu icin orada "OUTCOME: fail | cd" veriyordu
# — testin CI'da dusup lokalde gecmesi tam olarak bu yuzdendi (shell-harness
# CI-only-fail sinifi). Uretimde sonuc AYNI: automation/.. = /opt/linux-ai-server.
REPO_ROOT="${CODEX_FEED_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT" || { echo "OUTCOME: fail | cd"; exit 0; }

# UTF-8 locale ZORUNLU — baslik kirpmasi karakter-farkinda olmali (disc#1552).
# Cron ortaminda LANG genelde tanimsizdir; o zaman bash'in ${x:0:N} dilimlemesi
# BAYT tabanli calisir ve cok-baytli bir karakteri ORTASINDAN keser.
# C.UTF-8 yoksa asagidaki iconv guard'i zaten bozuk ciktiyi yayinlatmaz.
export LC_ALL=C.UTF-8

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
    # KARAKTER bazli kirpma — `cut -c` GNU'da locale'den BAGIMSIZ olarak BAYT
    # tabanlidir (-c ile -b ayni) ve cok-baytli karakteri ortasindan keserdi.
    # Belirti (disc#1552): "... altyapisi \xe2\x80" -> gecersiz UTF-8. Sonuc
    # tuketiciye gore degisiyordu: UTF-8 locale'de grep dosyayi binary sayip
    # Codex panelini SESSIZCE dusuruyordu; C locale'de Python tuketicileri
    # UnicodeDecodeError ile patliyordu. Bash dilimlemesi UTF-8 locale altinda
    # karakter-farkindadir (yukarida LC_ALL sabitlendi).
    short=${title:0:38}
    # NOT: tot = PR'daki TÜM Codex P1/P2 yorumu (çözülen dahil; unresolved-filtresi GraphQL ister, v1'de yok).
    # "açık" demiyoruz → DOĞRULA ile gerçek-durumu PR'da teyit ettiririz.
    echo "🤖 Codex: PR#${num} \"${short}\" — ${tot} bulgu (${p1} P1, ${p2} P2), DOĞRULA" >> "$TMP"
done <<< "$PRS"

# YAZ-ONCE-DOGRULA (disc#1552): bozuk UTF-8'i ASLA yayinlama. Gecersiz bayt
# iceren cache sessiz-arizaya yol aciyor — tuketici ya paneli dusuruyor ya
# patliyor, ikisi de tesahis edilmesi zor. Bozuksa eski cache korunur ve durum
# partial olarak RAPORLANIR (sessizce yutulmaz).
if ! iconv -f UTF-8 -t UTF-8 "$TMP" >/dev/null 2>&1; then
    rm -f "$TMP"
    echo "OUTCOME: partial | uretilen cache gecersiz UTF-8 (eski cache korundu, disc#1552)"
    exit 0
fi

mv "$TMP" "$OUT" 2>/dev/null || { echo "OUTCOME: partial | cache yazılamadı"; exit 0; }
echo "OUTCOME: pass | ${TOTAL_PR} açık PR, ${FLAGGED} Codex-bulgulu → cache güncel"
exit 0
