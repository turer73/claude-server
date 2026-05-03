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

# Stdin'den Claude Code hook input'u oku (cwd, session_id, hook_event_name)
# JSON parse fail veya jq yoksa graceful degrade — eski davranis (project filter yok)
HOOK_INPUT=$(cat 2>/dev/null)
CWD=$(echo "$HOOK_INPUT" | jq -r '.cwd // empty' 2>/dev/null)

# cwd → proje türevi. Yaygın repo kök yapıları:
#   /data/projects/<name>          → <name>
#   /opt/linux-ai-server[/...]     → linux-ai-server
#   /home/klipperos/work/<name>    → <name>
RAW_PROJECT=""
case "$CWD" in
  /data/projects/*)        RAW_PROJECT=$(echo "$CWD" | awk -F/ '{print $4}') ;;
  /opt/linux-ai-server*)   RAW_PROJECT="linux-ai-server" ;;
  /home/klipperos/work/*)  RAW_PROJECT=$(echo "$CWD" | awk -F/ '{print $5}') ;;
esac

# Fuzzy match için ilk segment (- ve . öncesi). Aile yakalar:
#   panola → panola, panola.app, panola-social, panola.com (DB project adlari)
#   bilge-arena → bilge → bilge-arena, bilgearena.com
#   linux-ai-server → linux → linux-ai-server
PROJECT_PREFIX=""
if [ -n "$RAW_PROJECT" ]; then
  PROJECT_PREFIX=$(echo "$RAW_PROJECT" | awk -F'[.-]' '{print $1}')
fi

{
  echo "=== HAFIZA SISTEMI — Oturum Baslangici ($DEV) ==="
  echo ""

  # Stats
  echo "Durum:"
  sqlite3 "$DB" "SELECT '  Hafiza: ' || COUNT(*) || ' kayit' FROM memories WHERE active=1;" 2>/dev/null
  sqlite3 "$DB" "SELECT '  Oturum: ' || COUNT(*) || ' toplam (' || (SELECT COUNT(*) FROM sessions WHERE device_name='$DEV') || ' bu cihaz)' FROM sessions;" 2>/dev/null
  sqlite3 "$DB" "SELECT '  Otonomi modu: ' || '$HOOK_AUTONOMY';" 2>/dev/null
  if [ -n "$PROJECT_PREFIX" ]; then
    echo "  Proje (cwd): $RAW_PROJECT (filter prefix: $PROJECT_PREFIX*)"
  fi

  # ─── Acik bug'lar — proje-bazli relevance + stale-filter ────────
  # Stale tanımı: 30+ gün açık + read_count=0 → büyük olasılıkla flake/obsolete,
  # session-start'ta gizle; LLM triage cron (memory-triage-llm.py) zaten temizleyecek.
  # /memory bugs ile tam liste hala erişilebilir.
  BUGS_TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active';" 2>/dev/null)

  if [ "${BUGS_TOTAL:-0}" -gt 0 ] && [ -n "$PROJECT_PREFIX" ]; then
    # Bu projedeki bug'lar — STALE FILTER YOK (proje bağlamı her zaman göster)
    PROJ_BUGS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active' AND project LIKE '${PROJECT_PREFIX}%';" 2>/dev/null)
    if [ "${PROJ_BUGS:-0}" -gt 0 ]; then
      echo ""
      echo "Bu Projedeki Bug'lar ($PROJ_BUGS):"
      sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND project LIKE '${PROJECT_PREFIX}%' ORDER BY created_at DESC LIMIT 7;" 2>/dev/null
    fi

    # Diğer projeler — STALE FILTER (30+ gün unread'leri çıkar)
    OTHER_BUGS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active' AND project NOT LIKE '${PROJECT_PREFIX}%' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0);" 2>/dev/null)
    if [ "${OTHER_BUGS:-0}" -gt 0 ]; then
      echo ""
      echo "Diğer Açık Bug'lar ($OTHER_BUGS, stale filtreli):"
      sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND project NOT LIKE '${PROJECT_PREFIX}%' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0) ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
    fi
  elif [ "${BUGS_TOTAL:-0}" -gt 0 ]; then
    # Proje türetilemedi — eski davranış, stale filter ile
    echo ""
    echo "Acik Bug'lar ($BUGS_TOTAL, stale filtreli):"
    sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0) ORDER BY created_at DESC LIMIT 10;" 2>/dev/null
  fi

  # ─── Aktif planlar — aynı project relevance ─────────────────────
  PLANS_TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active';" 2>/dev/null)
  if [ "${PLANS_TOTAL:-0}" -gt 0 ] && [ -n "$PROJECT_PREFIX" ]; then
    PROJ_PLANS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active' AND project LIKE '${PROJECT_PREFIX}%';" 2>/dev/null)
    if [ "${PROJ_PLANS:-0}" -gt 0 ]; then
      echo ""
      echo "Bu Projedeki Planlar ($PROJ_PLANS):"
      sqlite3 "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='plan' AND status='active' AND project LIKE '${PROJECT_PREFIX}%' ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
    fi
  elif [ "${PLANS_TOTAL:-0}" -gt 0 ]; then
    echo ""
    echo "Aktif Planlar ($PLANS_TOTAL):"
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
