#!/bin/bash
# Claude Code hook — oturum başında hafıza durumu kontrol
# Bu script'in çıktısı Claude'un context'ine eklenir
DB=/opt/linux-ai-server/data/claude_memory.db

echo "=== HAFIZA SİSTEMİ — Oturum Başlangıcı ==="
echo ""

# Stats
echo "📊 Durum:"
sqlite3 "$DB" "SELECT '  Hafıza: ' || COUNT(*) || ' kayıt' FROM memories WHERE active=1;"
sqlite3 "$DB" "SELECT '  Oturum: ' || COUNT(*) || ' toplam (' || (SELECT COUNT(*) FROM sessions WHERE device_name='klipper') || ' klipper)' FROM sessions;"
sqlite3 "$DB" "SELECT '  Cihaz: ' || COUNT(*) FROM devices;"

# Open bugs
BUGS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND resolved=0;")
if [ "$BUGS" -gt 0 ]; then
    echo ""
    echo "🐛 Açık Bug'lar ($BUGS):"
    sqlite3 "$DB" "SELECT '  [' || project || '] ' || title FROM discoveries WHERE type='bug' AND resolved=0;"
fi

# Unread notes
NOTES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='klipper' OR to_device IS NULL) AND read=0;")
if [ "$NOTES" -gt 0 ]; then
    echo ""
    echo "📬 Okunmamış Notlar ($NOTES):"
    sqlite3 "$DB" "SELECT '  ' || from_device || ': ' || title || ' — ' || substr(content,1,80) FROM notes WHERE (to_device='klipper' OR to_device IS NULL) AND read=0;"
fi

# Son 3 oturum
echo ""
echo "📋 Son Oturumlar:"
sqlite3 "$DB" "SELECT '  #' || session_num || ' (' || device_name || ', ' || date || '): ' || substr(summary,1,70) FROM sessions ORDER BY id DESC LIMIT 3;"

echo ""
echo "💡 /memory — dashboard | /memory save — oturum kaydet | /memory bug — bug kaydet"
