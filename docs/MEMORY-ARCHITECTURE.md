# Merkezi Hafiza Sistemi - Mimari

Linux-AI Server uzerinde calisan SQLite + REST API tabanli, multi-device,
Claude Code hook entegre hafiza sistemi.

## Genel Bakis

```
+------------------+      HTTP API       +------------------+
| Claude Code      |<------------------->| Linux-AI Server  |
| (Windows host)   |  X-Memory-Key auth  | (Klipper)        |
| 5 hook script    |                     | FastAPI :8420    |
+------------------+                     +------------------+
                                                  |
                                                  v
                                          +---------------+
                                          | SQLite DB     |
                                          | claude_memory |
                                          | 11 tablo      |
                                          +---------------+
```

## Bilesenler

### 1. Klipper Backend
- **API:** `/api/v1/memory/*` - 32 endpoint
- **DB:** `/opt/linux-ai-server/data/claude_memory.db`
- **Auth:** `X-Memory-Key` header
- **Service:** systemd `linux-ai-server.service`, port 8420, 2 worker
- **Lifespan telemetry:** `subprocess.Popen` startup + shutdown event POST

### 2. Klipper-side Scripts (`/opt/linux-ai-server/scripts/`)
| Dosya | Amac |
|-------|------|
| `claude-memory.sh` | Multi-device DB query helper (sqlite3 dogrudan) |
| `memory-session-start.sh` | Klipper-side SessionStart context inject (alternatif) |
| `klipper-event.sh` | systemd/cron event'lerini /tasks POST helper |
| `klipper-cron-wrap.sh` | Cron komutlarini saran wrapper, OK/FAIL POST |

### 3. Klipper-side Hooks (`/opt/linux-ai-server/scripts/hooks/`)
Apr 26'da yaratilmis, **Windows'a port edildi**:
| Bash | Python (Windows) |
|------|------------------|
| `pre-bash-guard.sh` | `pre-bash-guard.py` |
| `post-bash-capture.sh` | `post-bash-capture.py` |
| `user-prompt-log.sh` | `user-prompt-log.py` |
| `stop-save-session.py` | `memory-stop-hook.py` (basit versiyon, transcript ozet eklenebilir) |
| `session-start.sh` | `memory-hook.py` (API based, Klipper alternatif sqlite3 based) |

### 4. Windows-side Hooks (`~/.claude/hooks/`)
SessionStart, Stop, UserPromptSubmit, PreToolUse:Bash, PostToolUse:Bash - 5 event aktif.

### 5. Auto-memory (`~/.claude/projects/F--projelerim/memory/`)
- `MEMORY.md` index
- `feedback_*.md`, `project_*.md`, `reference_*.md` dosyalari
- `memory-consolidate.py` ile yeni dosyalar otomatik index'e eklenir
- 6 saatte 1 Windows scheduled task ile otomatik regenerate

## Veri Akisi

### Oturum Acilis
```
Claude Code start
  -> SessionStart hook tetiklenir
  -> memory-hook.py
  -> GET /api/v1/memory/sessions, /tasks, /memories (limit kirpilmis)
  -> stdout markdown output
  -> Claude context'ine enjekte
```

### Tool Kullanim (Bash)
```
Claude Bash command calistirmadan once
  -> PreToolUse:Bash hook tetiklenir
  -> pre-bash-guard.py
  -> 25 yikici desen kontrol
  -> match varsa exit 2 (komut bloklа)
  -> aksi halde komut calisir

Komut bittikten sonra
  -> PostToolUse:Bash hook tetiklenir
  -> post-bash-capture.py
  -> trigger pattern (pytest, npm, tsc, ...) kontrol
  -> match varsa local TSV log + rc!=0 ise discoveries.bug POST
```

### Kullanici Mesaji
```
Kullanici prompt yazar
  -> UserPromptSubmit hook tetiklenir
  -> user-prompt-log.py
  -> ~/.claude/hooks-logs/user-prompts.tsv'ye satir
```

### Oturum Kapanis
```
Claude oturum bitiminde
  -> Stop hook tetiklenir
  -> memory-stop-hook.py
  -> Transcript'i tara (sessions/memories/tasks POST count)
  -> Eksikse decision:block (Claude'u POST atmaya zorla)
  -> Buyuk ihtimalle 2. cagrida fallback POST
  -> fire_consolidate() subprocess.Popen
  -> memory-consolidate.py arka planda MEMORY.md guncelle
```

### Klipper Olaylari
```
systemd service-start/stop
  -> FastAPI lifespan icindeki subprocess.Popen
  -> /opt/linux-ai-server/scripts/klipper-event.sh
  -> /api/v1/memory/tasks POST (device_name=klipper)

cron entry (daily-backup, test-runner, weekly-audit, demo-reset, e2e-live, pull-vps)
  -> klipper-cron-wrap.sh wrapper
  -> komut calistir
  -> rc=0 ise OK, rc!=0 ise FAIL
  -> klipper-event.sh ile /tasks POST
```

## Cihazlar (4)

| name | platform | tailscale_ip | aktif |
|------|----------|--------------|-------|
| `klipper` | linux | 100.113.153.62 | ✓ canli |
| `windows-masaustu` | windows | - | ✓ canli |
| `windows-laptop` | windows | - | ⚠ stale (Apr 26) |
| `android-telefon` | android | - | ⚠ stale (Apr 03) |

## DB Tablolari

| Tablo | Aktif Kayit | Aciklama |
|-------|-------------|----------|
| memories | 380+ | feedback, project, reference, user |
| sessions | 360+ | Oturum kayitlari (multi-device) |
| tasks_log | 670+ | Gorev kayitlari |
| discoveries | 273+ | bug, fix, plan, architecture, workaround, learning, config |
| devices | 4 | Cihaz kayitlari + last_seen |
| device_projects | 11 | Cihaz-proje yol mapping |
| notes | 1 | Multi-device async messaging |
| task_queue | 5 | Multi-device task delegation |
| command_log | 0 | PostToolUse hook target (henuz dolmamis) |
| csp_violations | 1 | Web security |
| discoveries_fts | - | FTS index |

## Maintenance

| Job | Cadence | Hedef |
|-----|---------|-------|
| `archive-stale` cron | gunluk 02:30 | 90+ gun eski okunmamis discoveries -> obsolete |
| `memory-consolidate-periodic` | 6h | MEMORY.md regenerate |
| Cron telemetry | her cron sonu | tasks_log'a OK/FAIL kaydi |

## API Endpoint Ozet

```
GET  /api/v1/memory/dashboard          - Stats + cihazlar + son oturumlar
GET  /api/v1/memory/sessions           - Liste
POST /api/v1/memory/sessions           - Yeni oturum
GET  /api/v1/memory/memories           - Liste (type filter)
POST /api/v1/memory/memories           - Yeni memory
GET  /api/v1/memory/tasks              - Liste (project filter)
POST /api/v1/memory/tasks              - Yeni task
GET  /api/v1/memory/discoveries        - Liste (project filter)
POST /api/v1/memory/discoveries        - Yeni discovery
PUT  /api/v1/memory/discoveries/{id}/resolve - Cozuldu
GET  /api/v1/memory/devices            - Cihazlar
POST /api/v1/memory/devices/{name}/ping - last_seen guncelle
GET  /api/v1/memory/notes              - Multi-device notlar
POST /api/v1/memory/notes              - Not birak
GET  /api/v1/memory/queue              - Task queue
GET  /api/v1/memory/search?q=K         - FTS arama
POST /api/v1/memory/maintenance/archive-stale - Eski temizle
GET  /api/v1/memory/health             - Health check
GET  /health                           - Public Docker healthcheck
```

## Kabul Edilmis Eksikler

1. **MEMORY.md harness cache**: Claude Code internals "2 days old" cache'i bizim manuel ekleme'lerimizle senkronize degil. Sonraki oturum doku regenerate ile kapatilir.
2. **2 worker -> 2 startup event**: uvicorn `--workers 2` -> 2 lifespan -> 2 service-start kaydi. Gurultu ama dogru audit.
3. **android/laptop POST atmiyor**: Yalnizca klipper + windows-masaustu aktif. Diger 2 cihaz icin wrapper script gerek.
4. **Anthropic native `memory_20250818`**: Apr 23 2026'da launched. Bu sistem alternatif. Gelecekte hibrit/migrate karari verilebilir.
5. **claude-mem semantic search (ChromaDB)**: SQLite FTS keyword tabanli; semantic embeddings yok.

## Sonraki Adimlar (opsiyonel)

- Worker dedup (file-lock, 5s window)
- ChromaDB semantic search
- Anthropic memory tool entegrasyonu (DevOps Agent icin)
- Multi-device wrapper (android/laptop curl helper)
- /search FTS skill Claude Code skill'i (Windows-side eklendi `~/.claude/skills/memory/`)
