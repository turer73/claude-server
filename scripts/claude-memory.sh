#!/bin/bash
# Claude Memory DB — Multi-device query helper
# Kullanım: claude-memory.sh <komut> [parametreler]
DB=/opt/linux-ai-server/data/claude_memory.db

case "$1" in
  memories|m)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev, substr(description,1,50) as desc FROM memories WHERE active=1 AND type='$2' ORDER BY updated_at DESC;"
    else
      sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev, substr(description,1,50) as desc FROM memories WHERE active=1 ORDER BY type, name;"
    fi
    ;;
  memory|get)
    sqlite3 -header -column "$DB" "SELECT * FROM memories WHERE id=$2;"
    ;;
  sessions|s)
    if [ -n "$2" ] && [ "$2" != "${2//[!0-9]/}" ]; then
      # Device name filter
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
    sqlite3 -header -column "$DB" "SELECT project, type, title, resolved FROM discoveries WHERE session_id=(SELECT id FROM sessions WHERE session_num=$2);"
    ;;
  tasks|t)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, substr(task,1,50) as task, status, date(created_at) as date FROM tasks_log WHERE project='$2' ORDER BY created_at DESC LIMIT 20;"
    else
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, substr(task,1,50) as task, date(created_at) as date FROM tasks_log ORDER BY created_at DESC LIMIT 20;"
    fi
    ;;
  bugs|b)
    sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, title, substr(details,1,60) as details FROM discoveries WHERE type='bug' AND resolved=0 ORDER BY created_at DESC;"
    ;;
  discoveries|d)
    if [ -n "$2" ]; then
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, type, title, resolved, date(created_at) as date FROM discoveries WHERE project='$2' ORDER BY created_at DESC;"
    else
      sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, type, title, resolved FROM discoveries ORDER BY created_at DESC LIMIT 20;"
    fi
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
    echo "=== Memories ==="
    sqlite3 -header -column "$DB" "SELECT id, type, name, source_device as dev FROM memories WHERE active=1 AND (content LIKE '%$QUERY%' OR name LIKE '%$QUERY%') LIMIT 10;"
    echo ""
    echo "=== Discoveries ==="
    sqlite3 -header -column "$DB" "SELECT id, device_name as dev, project, type, title FROM discoveries WHERE title LIKE '%$QUERY%' OR details LIKE '%$QUERY%' LIMIT 10;"
    echo ""
    echo "=== Sessions ==="
    sqlite3 -header -column "$DB" "SELECT session_num as '#', date, device_name as dev, substr(summary,1,60) FROM sessions WHERE summary LIKE '%$QUERY%' OR tasks_completed LIKE '%$QUERY%' LIMIT 5;"
    ;;
  stats)
    echo "=== Claude Memory Stats ==="
    sqlite3 "$DB" "SELECT 'Memories: ' || COUNT(*) FROM memories WHERE active=1;"
    sqlite3 "$DB" "SELECT 'Sessions: ' || COUNT(*) FROM sessions;"
    sqlite3 "$DB" "SELECT 'Tasks: ' || COUNT(*) FROM tasks_log;"
    sqlite3 "$DB" "SELECT 'Discoveries: ' || COUNT(*) FROM discoveries;"
    sqlite3 "$DB" "SELECT 'Open bugs: ' || COUNT(*) FROM discoveries WHERE type='bug' AND resolved=0;"
    sqlite3 "$DB" "SELECT 'Devices: ' || COUNT(*) FROM devices;"
    sqlite3 "$DB" "SELECT 'Unread notes: ' || COUNT(*) FROM notes WHERE read=0;"
    echo ""
    echo "=== Per Device ==="
    sqlite3 -header -column "$DB" "SELECT d.name, d.platform, COUNT(DISTINCT s.id) as sessions, (SELECT COUNT(*) FROM tasks_log t WHERE t.device_name=d.name) as tasks FROM devices d LEFT JOIN sessions s ON s.device_name=d.name GROUP BY d.name ORDER BY sessions DESC;"
    ;;
  *)
    echo "Claude Memory DB — Multi-Device Hafıza Sistemi"
    echo ""
    echo "  $0 memories [type]     — Hafıza listesi (user/feedback/project/reference)"
    echo "  $0 get <id>            — Hafıza detayı"
    echo "  $0 sessions [dev|num]  — Oturum listesi (cihaz filtreli)"
    echo "  $0 session <num>       — Oturum detayı + tasks + discoveries"
    echo "  $0 tasks [project]     — Görev listesi"
    echo "  $0 bugs                — Açık bug'lar"
    echo "  $0 discoveries [proj]  — Keşifler"
    echo "  $0 devices             — Kayıtlı cihazlar"
    echo "  $0 notes [unread]      — Cihazlar arası notlar"
    echo "  $0 projects            — Cihaz-proje eşleşmeleri"
    echo "  $0 search <keyword>    — Tüm tablolarda arama"
    echo "  $0 stats               — İstatistikler"
    ;;
esac
