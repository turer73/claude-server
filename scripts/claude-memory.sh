#!/bin/bash
# Claude Memory DB v2 — Multi-device query helper
# Kullanım: claude-memory.sh <komut> [parametreler]
DB=/opt/linux-ai-server/data/claude_memory.db

case "$1" in
  memories|m)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev, read_count as reads, substr(description,1,50) as desc FROM memories WHERE active=1 AND type='$2' ORDER BY updated_at DESC;"
    else
      sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev, read_count as reads, substr(description,1,50) as desc FROM memories WHERE active=1 ORDER BY type, name;"
    fi
    ;;
  memory|get)
    sqlite3 -header -column "$DB" "SELECT * FROM memories WHERE id=$2;"
    sqlite3 "$DB" "UPDATE memories SET read_count=read_count+1, last_read_at=datetime('now') WHERE id=$2;"
    ;;
  sessions|s)
    if [ -n "$2" ] && [ "$2" != "${2//[!0-9]/}" ]; then
      sqlite3 -header -column "$DB" "SELECT session_num as '#', date, device_name as device, platform, substr(summary,1,60) as summary FROM sessions WHERE device_name='$2' ORDER BY id DESC LIMIT 20;"
    else
      sqlite3 -header -column "$DB" "SELECT session_num as '#', date, device_name as device, platform, substr(summary,1,60) as summary FROM sessions ORDER BY id DESC LIMIT ${2:-10};"
    fi
    ;;
  session)
    sqlite3 -header -column "$DB" "SELECT * FROM sessions WHERE session_num=$2;"
    echo ""
    echo "=== Tasks ==="
    sqlite3 -header -column "$DB" "SELECT project, task, status FROM tasks_log WHERE session_id=(SELECT id FROM sessions WHERE session_num=$2);"
    echo ""
    echo "=== Discoveries ==="
    sqlite3 -header -column "$DB" "SELECT project, type, title, status FROM discoveries WHERE session_id=(SELECT id FROM sessions WHERE session_num=$2);"
    ;;
  tasks|t)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, substr(task,1,50) as task, status, date(created_at) as date FROM tasks_log WHERE project='$2' ORDER BY created_at DESC LIMIT 20;"
    else
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, substr(task,1,50) as task, date(created_at) as date FROM tasks_log ORDER BY created_at DESC LIMIT 20;"
    fi
    ;;
  bugs|b)
    sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, title, substr(details,1,60) as details FROM discoveries WHERE type='bug' AND status='active' ORDER BY created_at DESC;"
    ;;
  discoveries|d)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, type, title, status, read_count as reads FROM discoveries WHERE project='$2' ORDER BY type, created_at DESC;"
    else
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, type, title, status, read_count as reads FROM discoveries ORDER BY type, project LIMIT 30;"
    fi
    ;;
  architecture|arch)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, project, title, status, substr(details,1,60) as details FROM discoveries WHERE type='architecture' AND project='$2' ORDER BY project;"
    else
      sqlite3 -header -column "$DB" "SELECT id, project, title, status, substr(details,1,60) as details FROM discoveries WHERE type='architecture' ORDER BY project;"
    fi
    ;;
  plans|plan)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, project, title, status, substr(details,1,60) as details FROM discoveries WHERE type='plan' AND project='$2' ORDER BY project;"
    else
      sqlite3 -header -column "$DB" "SELECT id, project, title, status, substr(details,1,60) as details FROM discoveries WHERE type='plan' ORDER BY status, project;"
    fi
    ;;
  project|proj)
    if [ -z "$2" ]; then
      echo "=== Proje Listesi ==="
      sqlite3 -header -column "$DB" "SELECT project, COUNT(*) as total, SUM(CASE WHEN type='bug' AND status='active' THEN 1 ELSE 0 END) as bugs, SUM(CASE WHEN type='architecture' THEN 1 ELSE 0 END) as arch, SUM(CASE WHEN type='plan' AND status='active' THEN 1 ELSE 0 END) as plans, SUM(CASE WHEN type='fix' THEN 1 ELSE 0 END) as fixes FROM discoveries GROUP BY project ORDER BY total DESC;"
    else
      echo "=== $2 — Discoveries ==="
      sqlite3 -header -column "$DB" "SELECT id, type, title, status, read_count as reads, date(created_at) as date FROM discoveries WHERE project='$2' ORDER BY type, created_at DESC;"
      echo ""
      echo "=== $2 — Tasks (son 10) ==="
      sqlite3 -header -column "$DB" "SELECT id, substr(task,1,50) as task, status, date(created_at) as date FROM tasks_log WHERE project='$2' ORDER BY created_at DESC LIMIT 10;"
    fi
    ;;
  health|h)
    echo "=== Hafıza Sağlık Raporu ==="
    TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries;")
    NEVER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE read_count=0;")
    STALE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM discoveries WHERE status='active' AND created_at < datetime('now', '-60 days');")
    echo "Toplam discovery: $TOTAL"
    echo "Hiç okunmamış:   $NEVER ($(( NEVER * 100 / (TOTAL > 0 ? TOTAL : 1) ))%)"
    echo "60+ gün stale:   $STALE"
    echo ""
    echo "=== En Çok Okunan ==="
    sqlite3 -header -column "$DB" "SELECT id, project, type, title, read_count as reads FROM discoveries WHERE read_count > 0 ORDER BY read_count DESC LIMIT 5;"
    echo ""
    echo "=== Hiç Okunmayan (proje bazlı) ==="
    sqlite3 -header -column "$DB" "SELECT project, COUNT(*) as unread FROM discoveries WHERE read_count=0 GROUP BY project ORDER BY unread DESC;"
    ;;
  devices|dev)
    sqlite3 -header -column "$DB" "SELECT name, platform, hostname, tailscale_ip as tailscale, claude_version as claude, last_seen FROM devices ORDER BY last_seen DESC;"
    ;;
  notes|n)
    if [ "$2" = "unread" ]; then
      sqlite3 -header -column "$DB" "SELECT id, from_device as 'from', to_device as 'to', title, datetime(created_at) as time FROM notes WHERE read=0 ORDER BY created_at DESC;"
    else
      sqlite3 -header -column "$DB" "SELECT id, from_device as 'from', to_device as 'to', title, read, datetime(created_at) as time FROM notes ORDER BY created_at DESC LIMIT 20;"
    fi
    ;;
  projects|p)
    sqlite3 -header -column "$DB" "SELECT device_name as device, project, local_path as path, datetime(last_activity) as activity FROM device_projects ORDER BY device_name, project;"
    ;;
  search)
    shift
    QUERY="$*"
    echo "=== FTS Search: $QUERY ==="
    sqlite3 -header -column "$DB" "SELECT d.id, d.project, d.type, d.title, d.status FROM discoveries d JOIN discoveries_fts f ON d.id=f.rowid WHERE discoveries_fts MATCH '$QUERY' LIMIT 15;" 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "(FTS hata, LIKE ile arıyorum...)"
      sqlite3 -header -column "$DB" "SELECT id, project, type, title, status FROM discoveries WHERE title LIKE '%$QUERY%' OR details LIKE '%$QUERY%' LIMIT 15;"
    fi
    echo ""
    echo "=== Memories ==="
    sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev FROM memories WHERE active=1 AND (content LIKE '%$QUERY%' OR name LIKE '%$QUERY%') LIMIT 10;"
    ;;
  stats)
    echo "=== Claude Memory Stats ==="
    sqlite3 "$DB" "SELECT 'Discoveries: ' || COUNT(*) FROM discoveries;"
    sqlite3 "$DB" "SELECT '  Active: ' || COUNT(*) FROM discoveries WHERE status='active';"
    sqlite3 "$DB" "SELECT '  Completed: ' || COUNT(*) FROM discoveries WHERE status='completed';"
    sqlite3 "$DB" "SELECT '  Obsolete: ' || COUNT(*) FROM discoveries WHERE status='obsolete';"
    sqlite3 "$DB" "SELECT 'Memories: ' || COUNT(*) FROM memories WHERE active=1;"
    sqlite3 "$DB" "SELECT 'Sessions: ' || COUNT(*) FROM sessions;"
    sqlite3 "$DB" "SELECT 'Tasks: ' || COUNT(*) FROM tasks_log;"
    sqlite3 "$DB" "SELECT 'Open bugs: ' || COUNT(*) FROM discoveries WHERE type='bug' AND status='active';"
    sqlite3 "$DB" "SELECT 'Devices: ' || COUNT(*) FROM devices;"
    sqlite3 "$DB" "SELECT 'Unread notes: ' || COUNT(*) FROM notes WHERE read=0;"
    echo ""
    echo "=== Per Device ==="
    sqlite3 -header -column "$DB" "SELECT d.name, d.platform, COUNT(DISTINCT s.id) as sessions, (SELECT COUNT(*) FROM tasks_log t WHERE t.device_name=d.name) as tasks FROM devices d LEFT JOIN sessions s ON s.device_name=d.name GROUP BY d.name ORDER BY sessions DESC;"
    ;;
  resolve)
    if [ -z "$2" ]; then echo "Kullanım: $0 resolve <id>"; exit 1; fi
    sqlite3 "$DB" "UPDATE discoveries SET resolved=1, status='completed' WHERE id=$2;"
    echo "Discovery #$2 resolved/completed."
    ;;
  obsolete)
    if [ -z "$2" ]; then echo "Kullanım: $0 obsolete <id>"; exit 1; fi
    sqlite3 "$DB" "UPDATE discoveries SET status='obsolete' WHERE id=$2;"
    echo "Discovery #$2 → obsolete."
    ;;
  complete)
    if [ -z "$2" ]; then echo "Kullanım: $0 complete <id>"; exit 1; fi
    sqlite3 "$DB" "UPDATE discoveries SET status='completed', resolved=1 WHERE id=$2;"
    echo "Discovery #$2 → completed."
    ;;
  *)
    echo "Claude Memory DB v2 — Hafıza Sistemi"
    echo ""
    echo "  Sorgulama:"
    echo "  $0 memories [type]      — Hafıza listesi (user/feedback/project/reference)"
    echo "  $0 get <id>             — Hafıza detayı"
    echo "  $0 sessions [dev|num]   — Oturum listesi"
    echo "  $0 session <num>        — Oturum detayı"
    echo "  $0 tasks [project]      — Görev listesi"
    echo "  $0 bugs                 — Açık bug'lar"
    echo "  $0 discoveries [proj]   — Tüm keşifler"
    echo "  $0 architecture [proj]  — Mimari kararlar"
    echo "  $0 plans [proj]         — Planlar"
    echo "  $0 project [name]       — Proje özeti (boş=liste)"
    echo "  $0 devices              — Cihazlar"
    echo "  $0 notes [unread]       — Notlar"
    echo "  $0 projects             — Cihaz-proje eşleşmeleri"
    echo "  $0 search <keyword>     — FTS arama"
    echo "  $0 stats                — İstatistikler"
    echo "  $0 health               — Sağlık raporu"
    echo ""
    echo "  Yönetim:"
    echo "  $0 resolve <id>         — Bug/fix çözüldü"
    echo "  $0 complete <id>        — Plan tamamlandı"
    echo "  $0 obsolete <id>        — Artık geçersiz"
    ;;
esac
