#!/bin/bash
# lint-cron-outcome.sh — Duyusal-dürüstlük lint'i (LIVESYS-SENSE).
#
# automation/crontab'ta klipper-cron-wrap.sh ile sarılan HER iş, OUTCOME-contract'a
# uymalı: ya kendisi (ya da çağırdığı .py/.sh) `OUTCOME:`/`emit_outcome` emit etmeli,
# ya da bilinçli olarak allowlist'te (tools/cron-outcome-allowlist.txt) olmalı. Aksi
# halde wrapper rc-fallback'e biner → sessiz-yeşil riski. İhlal varsa exit 1.
#
# Repo-kökünden çalışır (CI). crontab'taki /opt/linux-ai-server/ önekini repo-köküne map'ler.
set -uo pipefail

# ROOT/CRONTAB/ALLOWLIST env-override edilebilir (test fixture'ı için); default = repo.
ROOT="${LINT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CRONTAB="${LINT_CRONTAB:-$ROOT/automation/crontab}"
ALLOWLIST="${LINT_ALLOWLIST:-$ROOT/tools/cron-outcome-allowlist.txt}"
PREFIX="/opt/linux-ai-server/"

[ -f "$CRONTAB" ] || { echo "LINT-FATAL: crontab yok: $CRONTAB" >&2; exit 2; }

# allowlist (yorum/boş satır hariç basename listesi)
allowed() {
    [ -f "$ALLOWLIST" ] || return 1
    grep -vE '^\s*(#|$)' "$ALLOWLIST" | grep -qxF "$1"
}

# Bir dosyada (ve exec/python/bash ile çağırdığı .py/.sh'lerde) OUTCOME kanıtı var mı?
has_outcome() {
    local rel="$1" abs="$ROOT/$1"
    [ -f "$abs" ] || return 1
    grep -qE 'OUTCOME:|emit_outcome|outcome\.sh' "$abs" && return 0
    # exec/python/bash ile çağrılan repo-içi script'leri takip et (1 seviye)
    local sub
    for sub in $(grep -oE '[A-Za-z0-9_./-]+\.(py|sh)' "$abs" | sort -u); do
        local subrel="${sub#"$PREFIX"}"
        subrel="${subrel#./}"
        [ "$subrel" = "$rel" ] && continue
        [ -f "$ROOT/$subrel" ] && grep -qE 'OUTCOME:|emit_outcome' "$ROOT/$subrel" && return 0
    done
    return 1
}

violations=0
checked=0
# crontab'ta klipper-cron-wrap.sh saran satırlar; sarılan script = ilk .sh/.py mutlak yol
while IFS= read -r line; do
    case "$line" in \#* | '') continue ;; esac
    case "$line" in *klipper-cron-wrap.sh*) ;; *) continue ;; esac
    # cron-wrap'ten SONRAKİ ilk repo-içi .sh/.py = sarılan script (wrapper'ın kendisini atla)
    rest="${line#*klipper-cron-wrap.sh }"
    target="$(printf '%s' "$rest" | grep -oE "${PREFIX}[A-Za-z0-9_./-]+\.(sh|py)" | head -1)"
    [ -n "$target" ] || continue
    rel="${target#"$PREFIX"}"
    checked=$((checked + 1))
    if has_outcome "$rel"; then
        continue
    elif allowed "$(basename "$rel")"; then
        echo "ALLOW $rel (allowlist — OUTCOME bekliyor)"
    else
        echo "FAIL  $rel — OUTCOME marker yok ve allowlist'te değil" >&2
        violations=$((violations + 1))
    fi
done < "$CRONTAB"

echo "lint-cron-outcome: $checked cron-wrap işi denetlendi, $violations ihlal."
[ "$violations" -eq 0 ] || exit 1
