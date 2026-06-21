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

  # ─── 🛰️ AJAN FEED — tüm ajan sinyalleri tek-bakış (Yaşayan Sistem Farkındalığı) ──
  # Kullanıcı (2026-06-21): "ortak sistem kur tüm ajanlardan gelen bilgileri toplayıp sana
  # bilgi verecek". agent-feed.sh = Haiku-verdict + Codex + alarm + not + tekrar-cron birleşik.
  # FAIL-SAFE: script yok/hata → atla (oturum-start bozulmaz).
  FEED_SH="${HOOK_AGENT_FEED:-/opt/linux-ai-server/scripts/agent-feed.sh}"
  if [ -x "$FEED_SH" ]; then
    bash "$FEED_SH" --device "$DEV" 2>/dev/null
    echo ""
  fi

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

  # ─── 🔁 Tekrarlayan hatalar (auto-bug + events recurrence, Slice C) ──
  # AUTO-alert bug'lardan kaynağı son 7g'de >=3 critical basanlar = tekrar eden sorun
  # ("bunu 3. kez görüyorum"). server.db ATTACH ile events sayımı. FAIL-SAFE: hata/eksik
  # DB -> sessiz atla (oturum-start ASLA bozulmaz). Mevcut bug-sorguları DEĞİŞMEDİ.
  SRV_DB="${HOOK_SERVER_DB:-/opt/linux-ai-server/data/server.db}"
  if [ -r "$SRV_DB" ]; then
    RECUR=$(sqlite3 "$DB" "ATTACH '${SRV_DB}' AS srv; SELECT '  #' || d.id || ' ' || d.title || ' (🔁' || (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) || 'x/7g)' FROM discoveries d WHERE d.type='bug' AND d.status='active' AND d.title LIKE 'AUTO-alert: %' AND (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) >= 3 ORDER BY d.created_at DESC LIMIT 5;" 2>/dev/null)
    if [ -n "$RECUR" ]; then
      echo ""
      echo "🔁 Tekrarlayan Hatalar (kök-neden incele):"
      echo "$RECUR"
    fi
  fi

  # ─── 🌡️ Açık sistem alarmları + canlı termal ───────────────────
  # Kullanıcı (2026-06-21): "bu alarmları direk görmen gerek" — CPU 88°C'yi kullanıcı söyledi,
  # oysa server.db'de critical-temperature alarmı vardı ama hook çekmiyordu. Şimdi çekiyor.
  # Çözülmemiş critical/warning, son 6h, kaynak+mesaj dedup (2-worker uvicorn çift-yazar).
  # FAIL-SAFE: hata/eksik DB -> sessiz atla (oturum-start ASLA bozulmaz).
  if [ -r "$SRV_DB" ]; then
    ALARMS=$(sqlite3 "$SRV_DB" "SELECT '  [' || severity || '] ' || source || ': ' || substr(message,1,48) || '  (×' || COUNT(*) || ', son ' || datetime(MAX(timestamp),'localtime') || ')' FROM alerts WHERE resolved=0 AND timestamp > datetime('now','-6 hours') GROUP BY source, message ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, MAX(timestamp) DESC LIMIT 6;" 2>/dev/null)
    if [ -n "$ALARMS" ]; then
      echo ""
      echo "🌡️ Acik Sistem Alarmlari (server.db, cozulmemis, son 6h):"
      echo "$ALARMS"
    fi
  fi
  # Canlı CPU sıcaklığı (k10temp Tctl) — alarm-satırı olmasa bile mevcut durumu + runaway erken-uyarı.
  K10=$(for h in /sys/class/hwmon/hwmon*; do [ "$(cat "$h/name" 2>/dev/null)" = "k10temp" ] && echo "$h" && break; done)
  if [ -n "$K10" ]; then
    TCTL=""
    for t in "$K10"/temp*_input; do
      [ "$(cat "${t%_input}_label" 2>/dev/null)" = "Tctl" ] && TCTL=$(awk '{printf "%.0f",$1/1000}' "$t") && break
    done
    if [ -n "$TCTL" ]; then
      LOAD=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
      WARN=""
      [ "${TCTL:-0}" -ge 75 ] && WARN="  ⚠️ YUKSEK — runaway proses kontrol et: ps -eo pid,%cpu,etime,comm --sort=-%cpu | head"
      echo ""
      echo "🌡️ Canli: CPU ${TCTL}°C | yuk ${LOAD}${WARN}"
    fi
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

  # Okunmamis notlar — PER-DEVICE (#647): read_by varsa bu cihaza gore filtrele, yoksa legacy.
  HAS_RB=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('notes') WHERE name='read_by';" 2>/dev/null)
  if [ "${HAS_RB:-0}" -gt 0 ]; then
    UNREAD_PRED="read=0 AND (read_by IS NULL OR read_by NOT LIKE '%|$DEV|%')"
  else
    UNREAD_PRED="read=0"
  fi
  NOTES=$(sqlite3 "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $UNREAD_PRED;" 2>/dev/null)
  if [ "${NOTES:-0}" -gt 0 ]; then
    echo ""
    echo "Okunmamis Notlar ($NOTES):"
    sqlite3 "$DB" "SELECT '  ' || from_device || ': ' || title || ' — ' || substr(content,1,80) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $UNREAD_PRED ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
  fi

  # Son 3 oturum
  echo ""
  echo "Son Oturumlar:"
  sqlite3 "$DB" "SELECT '  #' || session_num || ' (' || device_name || ', ' || date || '): ' || substr(summary,1,70) FROM sessions ORDER BY id DESC LIMIT 3;" 2>/dev/null

  # Aktif feedback memoriler (top 8: read_count ASC = en az gorulen once)
  # Why: feedback memoriler claude'un davranisini sekillendirir; dormant kalmasinlar.
  # Read tracking icin de session basina bump.
  FEEDBACK_IDS=$(sqlite3 "$DB" "SELECT id FROM memories WHERE active=1 AND type='feedback' ORDER BY read_count ASC, updated_at DESC LIMIT 8;" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  if [ -n "$FEEDBACK_IDS" ]; then
    echo ""
    echo "Aktif Feedback (en az gorulen 8):"
    sqlite3 "$DB" "SELECT '  #' || id || ' [' || source_device || '] ' || name || ' — ' || substr(description,1,90) FROM memories WHERE id IN ($FEEDBACK_IDS) ORDER BY read_count, updated_at DESC;" 2>/dev/null
    # Read bump — bu feedback'leri context'e dahil ettik, gosterim sayilir
    sqlite3 "$DB" "UPDATE memories SET read_count=read_count+1, last_read_at=datetime('now') WHERE id IN ($FEEDBACK_IDS);" 2>/dev/null
  fi

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
