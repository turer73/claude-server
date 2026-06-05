#!/bin/bash
# Read-only SQL helper — data-analist (ve read_only /claude) için GÜVENLİ DB erişimi.
#
# GÜVENLİK: `sqlite3 -readonly` -> DB read-only açılır; herhangi bir yazma (INSERT/UPDATE/
# DELETE/DROP/ALTER) SQLite MOTORUNDA reddedilir ('attempt to write a readonly database').
# Pattern-eşleşmeye değil, ENGINE garantisine dayanır -> analist DELETE yazsa bile mutasyon
# OLMAZ. Yalnız allowlisted alias (server/coverage) -> başka DB/dosya açılamaz.
#
# Kullanım: db-query.sh <server|coverage> "<SQL>"
# Çıktı header+column, .timeout 5s, ~40KB cap (context-taşması önler; analist LIMIT kullanmalı).
set -u

ALIAS="${1:-}"
SQL="${2:-}"

case "$ALIAS" in
    server) DB="${DB_QUERY_SERVER:-/opt/linux-ai-server/data/server.db}" ;;
    coverage) DB="${DB_QUERY_COVERAGE:-/opt/linux-ai-server/data/coverage.db}" ;;
    *)
        echo "HATA: geçersiz db alias '$ALIAS' (izinli: server, coverage)" >&2
        exit 2
        ;;
esac
[ -f "$DB" ] || {
    echo "HATA: db yok: $DB" >&2
    exit 2
}
[ -n "$SQL" ] || {
    echo "HATA: SQL boş — kullanım: db-query.sh <server|coverage> \"<SQL>\"" >&2
    exit 2
}

# GÜVENLİK (Codex P1): dot-command'lar (.shell/.system/.output/.import/.load) -readonly'yi
# AŞAR (shell-exec/dosya-yazma/RCE). KANIT: '.shell echo X' shell çalıştırıyordu. İki katman:
# (1) '.' ile başlayan satırı REDDET (version-bağımsız), (2) sqlite3 -safe (motor düzeyinde
# tehlikeli dot-command + dosya-erişim + ATTACH'i kapatır).
if printf '%s' "$SQL" | grep -qE '^[[:space:]]*\.'; then
    echo "HATA: dot-command (satır-başı '.') yasak — yalnız SQL sorgusu (güvenlik)" >&2
    exit 2
fi

# -readonly: DB yazma motorda red. -safe: tehlikeli dot-command/dosya/ATTACH kapalı. head: cap.
sqlite3 -readonly -safe -header -column -cmd ".timeout 5000" "$DB" "$SQL" 2>&1 | head -c 40000
