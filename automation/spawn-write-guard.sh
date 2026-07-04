#!/bin/bash
# spawn-write-guard.sh — Spawn-isolation Faz-2 (P2-a çekirdek): PreToolUse deny-hook.
# Tasarım: docs/spawn-isolation-design.md §2.1. Spawn'ın Edit/Write'ı worktree-DIŞINA
# çıkamaz — cwd-umuduna değil DETERMINISTIK-enforce (absolute-path yazıları da yakalar).
#
# Claude Code hook-protokolü: stdin=tool-call JSON; exit 0=allow, exit 2=DENY
# (stderr Claude'a gösterilir → spawn path'ini düzeltebilir).
#
# Env: SPAWN_WT_PATH (zorunlu — per-spawn settings'e gömülür). İzinli-alanlar:
# worktree-subtree + /tmp (base-settings Write(//tmp/**) paritesi).
#
# FAIL-CLOSED: SPAWN_WT_PATH-boş / path-parse-edilemez / realpath-hata → DENY+mesaj.
# (§2.5 fail-open ilkesi ALTYAPI için; YAZI-GUARD güvenlik-katmanı — ters-varsayılan.)

set -uo pipefail

deny() { echo "spawn-write-guard DENY: $1" >&2; exit 2; }

WT="${SPAWN_WT_PATH:-}"
[ -n "$WT" ] || deny "SPAWN_WT_PATH tanımsız (izolasyon-konfig hatası) — yazma reddedildi"
WT_REAL=$(realpath -m "$WT" 2>/dev/null) || deny "worktree-yolu çözülemedi: $WT"

INPUT=$(cat 2>/dev/null || echo "")
# Edit/Write/MultiEdit: .tool_input.file_path — NotebookEdit: .tool_input.notebook_path
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null)
[ -n "$FILE" ] || deny "dosya-yolu parse-edilemedi (fail-closed) — Edit/Write worktree-içinde relative-path kullan"

# Relative-path: hook cwd'si spawn-cwd'sidir (worktree) → worktree'ye göre çöz.
case "$FILE" in
    /*) : ;;
    *)  FILE="$WT_REAL/$FILE" ;;
esac
FILE_REAL=$(realpath -m "$FILE" 2>/dev/null) || deny "hedef-yol çözülemedi: $FILE"

case "$FILE_REAL" in
    "$WT_REAL"/*|"$WT_REAL") exit 0 ;;                # worktree-içi → allow
    /tmp/*) exit 0 ;;                                  # base-settings Write(//tmp/**) paritesi
    *) deny "worktree-dışı yazma: $FILE_REAL (izinli: $WT_REAL/** ve /tmp/**) — ana-checkout'a yazamazsın (spawn-isolation §2.1)" ;;
esac
