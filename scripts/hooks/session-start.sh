#!/bin/bash
# SessionStart hook — Claude oturumun basinda hafiza durumunu enjekte eder
# Cikti Claude'un context'ine eklenir (additionalContext olarak)
HOOK_NAME=session-start
. "$(dirname "$0")/lib/common.sh"

DB="$HOOK_DB"
DEV="$HOOK_DEVICE"

# DB yoksa sessizce cik (hook hata vermemeli)
if [ ! -r "$DB" ]; then
  hook_log "DB okunamadi: $DB"
  exit 0
fi

# Stdin'i oku ama gerekli olan yok — sadece okuma
cat > /dev/null 2>&1 || true

{
  echo "=== HAFIZA SISTEMI — Oturum Baslangici ($DEV) ==="
  echo ""

  # Stats
  echo "Durum:"
  sqlite3 "$DB" "SELECT '  Hafiza: ' || COUNT(*) || ' kayit' FROM memories WHERE active=1;" 2>/dev/null
  sqlite3 "$DB" "SELECT '  Oturum: ' || COUNT(*) || ' toplam (' || (SELECT COUNT(*) FROM sessions WHERE device_name='$DEV') || ' bu cihaz)' FROM sessions;" 2>/dev/null
  sqlite3 "$DB" "SELECT '  Otonomi modu: ' || '$HOOK_AUTONOMY';" 2>/dev/null

  # Acik bug'lar
  BUGS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active';" 2>/dev/null)
  if [ "${BUGS:-0}" -gt 0 ]; then
    echo ""
    echo "Acik Bug'lar ($BUGS):"
    sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' ORDER BY created_at DESC LIMIT 10;" 2>/dev/null
  fi

  # Aktif planlar
  PLANS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active';" 2>/dev/null)
  if [ "${PLANS:-0}" -gt 0 ]; then
    echo ""
    echo "Aktif Planlar ($PLANS):"
    sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='plan' AND status='active' ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
  fi

  # Okunmamis notlar
  NOTES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND read=0;" 2>/dev/null)
  if [ "${NOTES:-0}" -gt 0 ]; then
    echo ""
    echo "Okunmamis Notlar ($NOTES):"
    sqlite3 "$DB" "SELECT '  ' || from_device || ': ' || title || ' — ' || substr(content,1,80) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND read=0 ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
  fi

  # Son 3 oturum
  echo ""
  echo "Son Oturumlar:"
  sqlite3 "$DB" "SELECT '  #' || session_num || ' (' || device_name || ', ' || date || '): ' || substr(summary,1,70) FROM sessions ORDER BY id DESC LIMIT 3;" 2>/dev/null

  # Son test/build sonuclari (hook ile yakalananlar)
  if [ -r "$HOOK_LOG_DIR/last-test-results.tsv" ]; then
    echo ""
    echo "Son Test/Build Sonuclari:"
    tail -n 5 "$HOOK_LOG_DIR/last-test-results.tsv" 2>/dev/null | awk -F'\t' '{printf "  %s  %s  rc=%s  %s\n", $1, $2, $3, $4}'
  fi

  echo ""
  echo "Komutlar: /memory dashboard | /memory save | /memory bug"
} 2>/dev/null

exit 0
