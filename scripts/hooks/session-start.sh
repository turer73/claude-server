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

  # ─── 📋 Gündem Panosu — /agenda tek-kaynak read-model (topic-3/P-D, PR#344) ──
  # agenda-endpoint'i tüketen tek-bakış özeti: cross-project yapılacaklar + kontrol-edilecekler.
  # Hook'un kendi ad-hoc SQL bloklarını (aşağıda) TAMAMLAR, tek-kaynak read-model'i yüzeye çıkarır.
  # FAIL-SAFE: servis down / key yok / jq yok / boş yanıt → sessizce atla (oturum-start bozulmaz).
  if [ -n "${MEMORY_API_KEY:-}" ] && command -v jq >/dev/null 2>&1; then
    AGENDA=$(curl -fsS --max-time 5 -H "X-Memory-Key: $MEMORY_API_KEY" "$HOOK_API/agenda" 2>/dev/null)
    if [ -n "$AGENDA" ]; then
      echo "$AGENDA" | jq -r '
        "📋 Gündem Panosu (tek-kaynak /agenda):",
        "  Yapılacaklar: \(.yapilacaklar.active_bugs|length) aktif bug · \(.yapilacaklar.open_discoveries|length) açık keşif · \(.yapilacaklar.pending_tasks|length) bekleyen görev · \(.yapilacaklar.open_claims|length) açık CLAIM",
        "  ⚠️ Kontrol: \(.kontrol_edilecekler.total_never_read) okunmamış-aktif · \(.kontrol_edilecekler.never_read_important|length) önemli-hiç-okunmamış(imp≥7) · \(.kontrol_edilecekler.stale_active_30d|length) stale-30g │ Ajan: \(.ajan_saglik.active_device_count) aktif/\(.ajan_saglik.silent_devices|length) sessiz"
      ' 2>/dev/null
      echo ""
    fi
  fi

  # ─── 🆕 Son oturumdan beri yeni sinyaller (watermark-delta, LSA Faz-2) ──
  # Watermark = bu cihazın SON kaydedilen oturumunun created_at'i. "Sen yokken ne oldu" deltası.
  # FAIL-SAFE: hata → atla. SRV_DB aşağıda da kullanılıyor (burada tanımla).
  SRV_DB="${HOOK_SERVER_DB:-/opt/linux-ai-server/data/server.db}"
  LASTSES=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT created_at FROM sessions WHERE device_name='$DEV' ORDER BY id DESC LIMIT 1;" 2>/dev/null)
  if [ -n "$LASTSES" ]; then
    NEW_DISC=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE created_at > '$LASTSES';" 2>/dev/null)
    NEW_NOTE=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM notes WHERE created_at > '$LASTSES' AND from_device != '$DEV';" 2>/dev/null)
    NEW_CRIT=$(sqlite3 -cmd ".timeout 5000" "$SRV_DB" "SELECT COUNT(*) FROM alerts WHERE timestamp > '$LASTSES' AND severity='critical';" 2>/dev/null)
    if [ "${NEW_DISC:-0}" -gt 0 ] || [ "${NEW_NOTE:-0}" -gt 0 ] || [ "${NEW_CRIT:-0}" -gt 0 ]; then
      echo "🆕 Son oturumdan beri (${LASTSES%%.*}): ${NEW_DISC:-0} yeni discovery, ${NEW_NOTE:-0} yeni not, ${NEW_CRIT:-0} kritik alarm"
      echo ""
    fi
  fi

  # ─── 📋 Son Ajan Bulguları / Yapılacaklar (Turgut 2026-07-18): "tüm ajanların
  # bulguları... yeni bir sistem kurulup tüm bulgular listelenip oraya kaydedilmeli,
  # her oturum başlangıcında öncelikli bakılmalı". Ajanlar (ad-advisor/adsense-
  # readiness/agent-health-report/data-analyst/vb.) bulgularını zaten discoveries'e
  # (type=learning) yazıyordu — ama SessionStart bunları hiç LİSTELEMİYORDU, yalnız
  # "N yeni discovery" SAYISI görünüyordu (yukarıdaki 🆕 satırı). agent-health-report.py
  # (ajanları-kontrol-eden-ajan) zaten stale/kronik-fail ajanları tespit edip haftalık
  # raporunu buraya yazıyor — eksik olan GÖRÜNÜRLÜKTÜ, yeni bir tespit-mekanizması değil.
  #
  # RAPOR-AİLESİ DEDUP (awk): günlük "Sistem Durumu"/"Tekrar Eden Pattern'ler" ikilisi
  # ham-kronolojik sıralamada 8-slotu ~4 günde doldurup haftalık raporları (Ajan Sağlığı
  # gibi) tamamen dışarı itiyordu (canlı-testte yakalandı). Başlıktaki tarih/hafta-eki
  # (' — 2026-07-18' / ' — 2026-W29') soyulup 'aile' çıkarılır; her aileden yalnız EN
  # GÜNCEL örnek gösterilir → günlük+haftalık raporlar birbirini boğmadan yan yana durur.
  # Proje-relevance önce (bugs deseniyle aynı), aile-içi en-yeni. read_count bump edilir
  # (analitik/ileride kullanım için) ama SIRALAMAYI YÖNLENDİRMEZ (least-read-first, eski-
  # hiç-gösterilmemiş bir kaydı bugünün raporunun önüne geçirirdi — "ne oldu ŞİMDİ" amacının
  # tersi; feedback-memories'ten BİLEREK farklı).
  LEARN_TOTAL=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='learning' AND status='active';" 2>/dev/null)
  if [ "${LEARN_TOTAL:-0}" -gt 0 ]; then
    if [ -n "$PROJECT_PREFIX" ]; then
      ORDERBY="CASE WHEN project LIKE '${PROJECT_PREFIX}%' THEN 0 ELSE 1 END, created_at DESC"
    else
      ORDERBY="created_at DESC"
    fi
    LEARN_IDS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT id || char(9) || title FROM discoveries WHERE type='learning' AND status='active' ORDER BY $ORDERBY;" 2>/dev/null |
      awk -F'\t' '{
        fam = $2
        sub(/ — [0-9]{4}-(W[0-9]{2}|[0-9]{2}-[0-9]{2})$/, "", fam)
        if (!(fam in seen)) { seen[fam] = 1; print $1; n++ }
        if (n >= 8) exit
      }' | tr '\n' ',' | sed 's/,$//')
    if [ -n "$LEARN_IDS" ]; then
      N_SHOWN=$(echo "$LEARN_IDS" | tr ',' '\n' | wc -l)
      echo "📋 Son Ajan Bulguları — ne oldu/ne yapılacak ($LEARN_TOTAL aktif, $N_SHOWN rapor-ailesi — /memory search ile tümü):"
      sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title || ' — ' || substr(REPLACE(COALESCE(details,''),char(10),' '),1,90) FROM discoveries WHERE id IN ($LEARN_IDS) ORDER BY $ORDERBY;" 2>/dev/null
      sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE discoveries SET read_count=COALESCE(read_count,0)+1, last_read_at=datetime('now') WHERE id IN ($LEARN_IDS);" 2>/dev/null
      echo ""
    fi
  fi

  # Stats
  echo "Durum:"
  sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  Hafiza: ' || COUNT(*) || ' kayit' FROM memories WHERE active=1;" 2>/dev/null
  sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  Oturum: ' || COUNT(*) || ' toplam (' || (SELECT COUNT(*) FROM sessions WHERE device_name='$DEV') || ' bu cihaz)' FROM sessions;" 2>/dev/null
  sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  Otonomi modu: ' || '$HOOK_AUTONOMY';" 2>/dev/null
  if [ -n "$PROJECT_PREFIX" ]; then
    echo "  Proje (cwd): $RAW_PROJECT (filter prefix: $PROJECT_PREFIX*)"
  fi

  # ─── Acik bug'lar — proje-bazli relevance + stale-filter ────────
  # Stale tanımı: 30+ gün açık + read_count=0 → büyük olasılıkla flake/obsolete,
  # session-start'ta gizle; LLM triage cron (memory-triage-llm.py) zaten temizleyecek.
  # /memory bugs ile tam liste hala erişilebilir.
  BUGS_TOTAL=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active';" 2>/dev/null)

  if [ "${BUGS_TOTAL:-0}" -gt 0 ] && [ -n "$PROJECT_PREFIX" ]; then
    # Bu projedeki bug'lar — STALE FILTER YOK (proje bağlamı her zaman göster)
    PROJ_BUGS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active' AND project LIKE '${PROJECT_PREFIX}%';" 2>/dev/null)
    if [ "${PROJ_BUGS:-0}" -gt 0 ]; then
      echo ""
      echo "Bu Projedeki Bug'lar ($PROJ_BUGS):"
      sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND project LIKE '${PROJECT_PREFIX}%' ORDER BY created_at DESC LIMIT 7;" 2>/dev/null
    fi

    # Diğer projeler — STALE FILTER (30+ gün unread'leri çıkar)
    OTHER_BUGS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active' AND project NOT LIKE '${PROJECT_PREFIX}%' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0);" 2>/dev/null)
    if [ "${OTHER_BUGS:-0}" -gt 0 ]; then
      echo ""
      echo "Diğer Açık Bug'lar ($OTHER_BUGS, stale filtreli):"
      sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND project NOT LIKE '${PROJECT_PREFIX}%' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0) ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
    fi
  elif [ "${BUGS_TOTAL:-0}" -gt 0 ]; then
    # Proje türetilemedi — eski davranış, stale filter ile
    echo ""
    echo "Acik Bug'lar ($BUGS_TOTAL, stale filtreli):"
    sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='bug' AND status='active' AND NOT (julianday('now') - julianday(created_at) > 30 AND read_count = 0) ORDER BY created_at DESC LIMIT 10;" 2>/dev/null
  fi

  # ─── 🔁 Tekrarlayan hatalar (auto-bug + events recurrence, Slice C) ──
  # AUTO-alert bug'lardan kaynağı son 7g'de >=3 critical basanlar = tekrar eden sorun
  # ("bunu 3. kez görüyorum"). server.db ATTACH ile events sayımı. FAIL-SAFE: hata/eksik
  # DB -> sessiz atla (oturum-start ASLA bozulmaz). Mevcut bug-sorguları DEĞİŞMEDİ.
  SRV_DB="${HOOK_SERVER_DB:-/opt/linux-ai-server/data/server.db}"
  if [ -r "$SRV_DB" ]; then
    RECUR=$(sqlite3 -cmd ".timeout 5000" "$DB" "ATTACH '${SRV_DB}' AS srv; SELECT '  #' || d.id || ' ' || d.title || ' (🔁' || (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) || 'x/7g)' FROM discoveries d WHERE d.type='bug' AND d.status='active' AND d.title LIKE 'AUTO-alert: %' AND (SELECT COUNT(*) FROM srv.events e WHERE e.source = substr(d.title,13) AND e.severity='critical' AND e.timestamp > datetime('now','-7 days')) >= 3 ORDER BY d.created_at DESC LIMIT 5;" 2>/dev/null)
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
    ALARMS=$(sqlite3 -cmd ".timeout 5000" "$SRV_DB" "SELECT '  [' || severity || '] ' || source || ': ' || substr(message,1,48) || '  (×' || COUNT(*) || ', son ' || datetime(MAX(timestamp),'localtime') || ')' FROM alerts WHERE resolved=0 AND timestamp > datetime('now','-6 hours') GROUP BY source, message ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, MAX(timestamp) DESC LIMIT 6;" 2>/dev/null)
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
  PLANS_TOTAL=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active';" 2>/dev/null)
  if [ "${PLANS_TOTAL:-0}" -gt 0 ] && [ -n "$PROJECT_PREFIX" ]; then
    PROJ_PLANS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active' AND project LIKE '${PROJECT_PREFIX}%';" 2>/dev/null)
    if [ "${PROJ_PLANS:-0}" -gt 0 ]; then
      echo ""
      echo "Bu Projedeki Planlar ($PROJ_PLANS):"
      sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='plan' AND status='active' AND project LIKE '${PROJECT_PREFIX}%' ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
    fi
  elif [ "${PLANS_TOTAL:-0}" -gt 0 ]; then
    echo ""
    echo "Aktif Planlar ($PLANS_TOTAL):"
    sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  [' || project || '] #' || id || ' ' || title FROM discoveries WHERE type='plan' AND status='active' ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
  fi

  # Okunmamis notlar — PER-DEVICE (#647): read_by varsa bu cihaza gore filtrele, yoksa legacy.
  HAS_RB=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM pragma_table_info('notes') WHERE name='read_by';" 2>/dev/null)
  if [ "${HAS_RB:-0}" -gt 0 ]; then
    UNREAD_PRED="read=0 AND (read_by IS NULL OR read_by NOT LIKE '%|$DEV|%')"
  else
    UNREAD_PRED="read=0"
  fi
  # Policy-gate #1222: held dispatch AKTIF-okunmamis listesinden CIKAR (teslim-filtresi, aksiyon-tetiklemesin)
  # AMA insana onay-icin AYRI bolumde goster (tasarim §4). Kolon-guard: status yoksa filtre-yok (geri-uyum).
  HAS_STATUS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM pragma_table_info('notes') WHERE name='status';" 2>/dev/null)
  [ "${HAS_STATUS:-0}" -gt 0 ] && UNREAD_PRED="$UNREAD_PRED AND COALESCE(status,'active')='active'"
  NOTES=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $UNREAD_PRED;" 2>/dev/null)
  if [ "${NOTES:-0}" -gt 0 ]; then
    echo ""
    echo "Okunmamis Notlar ($NOTES):"
    sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  ' || from_device || ': ' || title || ' — ' || substr(content,1,80) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND $UNREAD_PRED ORDER BY created_at DESC LIMIT 5;" 2>/dev/null
  fi

  # Policy-gate #1222 onay-gorunumu: held dispatch'ler INSANA onay-icin (approve/reject MASTER-key).
  # Aktif-listede DEGIL (aksiyon-tetiklemez); yalniz "onay-bekliyor" hatirlatmasi (tasarim §4, Telegram-DEGIL).
  if [ "${HAS_STATUS:-0}" -gt 0 ]; then
    HELD=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND status='held';" 2>/dev/null)
    if [ "${HELD:-0}" -gt 0 ]; then
      echo ""
      echo "Onay Bekleyen HELD Dispatch ($HELD) — otonom-consequential, approve/reject MASTER-key:"
      sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  #' || id || ' ' || from_device || '->' || COALESCE(to_device,'*') || ': ' || title FROM notes WHERE (to_device='$DEV' OR to_device IS NULL) AND status='held' ORDER BY created_at DESC LIMIT 10;" 2>/dev/null
    fi
  fi

  # Son 3 oturum
  echo ""
  echo "Son Oturumlar:"
  sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  #' || session_num || ' (' || device_name || ', ' || date || '): ' || substr(summary,1,70) FROM sessions ORDER BY id DESC LIMIT 3;" 2>/dev/null

  # Aktif feedback memoriler (top 8: read_count ASC = en az gorulen once)
  # Why: feedback memoriler claude'un davranisini sekillendirir; dormant kalmasinlar.
  # Read tracking icin de session basina bump.
  FEEDBACK_IDS=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT id FROM memories WHERE active=1 AND type='feedback' ORDER BY read_count ASC, updated_at DESC LIMIT 8;" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
  if [ -n "$FEEDBACK_IDS" ]; then
    echo ""
    echo "Aktif Feedback (en az gorulen 8):"
    sqlite3 -cmd ".timeout 5000" "$DB" "SELECT '  #' || id || ' [' || source_device || '] ' || name || ' — ' || substr(description,1,90) FROM memories WHERE id IN ($FEEDBACK_IDS) ORDER BY read_count, updated_at DESC;" 2>/dev/null
    # Read bump — bu feedback'leri context'e dahil ettik, gosterim sayilir
    sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE memories SET read_count=read_count+1, last_read_at=datetime('now') WHERE id IN ($FEEDBACK_IDS);" 2>/dev/null
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
