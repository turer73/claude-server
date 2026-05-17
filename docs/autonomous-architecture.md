# Klipper Otonom Not İşleme Mimarisi

**Son güncelleme:** 2026-05-18
**Sahip:** klipper-side
**Sorumlu agent:** Claude Opus 4.7 (interactive) + Claude Sonnet 4.6 (autonomous spawn) + Ollama qwen2.5:7b (local classifier)

## Amaç

Sürer (Windows) ve klipper (Linux) AI agent'ları arasındaki not (note) sisteminde, **kullanıcı uyurken bile** otonom not işleme yapılabilsin. İnsan kullanıcı prompt'una bağımlı olmadan, yeni gelen notlara akıllı ve güvenli tepki ver.

## Tasarım Prensipleri

1. **Çok katmanlı LLM kullanımı** — pahalı modelden ucuza önceliklendir
2. **Permission allowlist** — bypass YOK, denylist + allowlist
3. **Confidence-aware routing** — model emin değilse defer
4. **Tek-not disiplini** — autonomous spawn yalnız kendi note'unu işler
5. **Loop prevention** — autonomous Claude not göndermez (sürer'a yanıt yaz mez)
6. **Observability** — günlük summary + health check
7. **Reversible** — her commit / API yazma kolay revert edilebilir

## Mimari Bileşenler

```
┌─────────────────────────────────────────────────────────────────┐
│                    KULLANICI (Turgut)                            │
└────────┬────────────────────────────────────────┬────────────────┘
         │ prompt                              prompt
         ▼                                       ▼
┌─────────────────┐                    ┌──────────────────┐
│ Klipper Opus    │  ◄────── notes ───►│ Sürer Sonnet     │
│ (interactive)   │   Memory API       │ (interactive)    │
└────────┬────────┘                    └──────────────────┘
         │ writes
         ▼
┌──────────────────────────────────────────┐
│  Memory API (FastAPI :8420)              │
│  - /api/v1/memory/notes (CRUD)           │
│  - /api/v1/memory/memories (CRUD)        │
│  - /api/v1/classify/note (Ollama proxy)  │  ◄── Sürer kullanır
│  - SQLite /opt/.../claude_memory.db      │
└──────────────┬───────────────────────────┘
               │ poll every 30s
               ▼
┌──────────────────────────────────────────┐
│  note-poller.service (systemd, klipperos)│
│  - SQLite poll                            │
│  - State: last_seen_id                    │
│  - On new → spawn autonomous-claude.sh    │
└──────────────┬───────────────────────────┘
               │ background
               ▼
┌──────────────────────────────────────────┐
│  autonomous-claude.sh (TIER 1: Classify) │
│  ├─ Lock (/tmp/klipper-autonomous-...)   │
│  ├─ Throttle (60s min)                    │
│  ├─ Interactive detection (default OFF)   │
│  └─ Call autonomous-classifier-v2.sh      │
└──────────────┬───────────────────────────┘
               │
       ┌───────┴──────────┐
       │ qwen2.5:7b call  │  ~3sn, $0
       │ → LABEL + CONF   │
       └───────┬──────────┘
               │
       ┌───────┴────────┬─────────┬─────────┐
       ▼                ▼         ▼         ▼
    ┌─────┐        ┌────────┐ ┌─────┐ ┌──────┐
    │ ACK │        │ ACTION │ │ DIS │ │ URG  │
    └──┬──┘        │ -ABLE  │ │CUSS │ │ ENT  │
       │           └────┬───┘ └──┬──┘ └──┬───┘
   local handle:       │         │       │
   mark read +         │      defer:  alert +
   memory entry        │     unread,  memory +
   (Claude YOK,        │     kullanıcı  unread
    $0)                │     görür      kullanıcı
                       │                görür
                       │
              LOW conf? ──→ override → defer (safety)
                       │
                       ▼
           ┌─────────────────────────────────┐
           │ claude -p (Sonnet 4.6, Max plan)│
           │ --settings allowlist.json       │
           │ --append-system-prompt          │
           │   guardrails.md                 │
           │                                  │
           │ Allow: Read, Edit, Write (paths),│
           │  git local (no push), tests,     │
           │  sqlite3, internal API curl      │
           │ Deny: sudo, rm, dd, docker, ssh, │
           │  scp, git push, vps-run.sh,      │
           │  WebFetch, WebSearch             │
           └─────────────┬───────────────────┘
                         │ uses
                         ▼
                Memory API (mark read, write entry)
```

## Dosya Envanteri

### Çalıştırılabilir scriptler
- `automation/note-poller.sh` — daemon (systemd)
- `automation/autonomous-claude.sh` — TIER router
- `automation/autonomous-classifier-v2.sh` — Ollama 4-class + confidence
- `automation/autonomous-classifier.sh` — v1 (deprecated, v2'ye geçildi)
- `automation/autonomous-daily-summary.sh` — cron 06:00
- `automation/autonomous-health-check.sh` — cron her 4 saat

### Configuration
- `automation/autonomous-claude-settings.json` — claude permissions (allowlist + denylist)
- `automation/autonomous-claude-guardrails.md` — system prompt (TEK-NOTE DİSİPLİNİ + güvenlik)
- `/etc/systemd/system/klipper-note-poller.service`
- `/etc/edge-log-salt` (mod 600) — IP pseudonymize için, otonom mod kullanmaz

### Hook entegrasyonu
- `scripts/hooks/session-start.sh` — SessionStart: unread inject
- `scripts/hooks/user-prompt-messages.sh` — UserPromptSubmit: in-session note inject
- `scripts/hooks/stop-check-inbox.py` — Stop: turn-end unread block (kullanıcı kapatmadan işle)
- `scripts/hooks/stop-save-session.py` — Stop: session save (memory)
- `scripts/hooks/post-edit-capture.sh` — PostToolUse Write|Edit: dosya tracking
- `scripts/hooks/post-compact-save.sh` — PostCompact: context loss prevention

### API endpoints (FastAPI)
- `POST /api/v1/memory/notes` — yeni not
- `PUT /api/v1/memory/notes/{id}/read` — okundu mark
- `PUT /api/v1/memory/notes/{id}/unread` — geri unread (test/debug)
- `GET /api/v1/memory/notes?device=X&unread_only=true` — filter
- `POST /api/v1/classify/note` — Ollama classifier proxy (sürer kullanır)
- `POST /api/v1/memory/memories` — memory entry yaz
- `PUT /api/v1/memory/memories/{id}` — memory update

## Maliyet ve Performans

### Maliyet Modeli

| Bileşen | Tier | Maliyet |
|---|---|---|
| Ollama qwen2.5:7b classifier | Local | $0 (electric) |
| Claude Sonnet 4.6 spawn (ACTIONABLE) | Max plan | $0 marjinal (subscription) |
| SQLite sorgu | Local | $0 |
| FastAPI request | Local | $0 |

**Maksimum maruz kalma:** 50 spawn/gün × ~$0.15 effective = $7.50 "report only" cost. Max plan subscription'a dahil, kullanıcıya yansımaz. Quota: ~225 mesaj/5h Max x20 ile sınır.

### Performans

- Classifier (warm): ~3sn
- ACK local handle: ~1sn
- ACTIONABLE spawn ortalaması: 30-60sn (sonnet, 8-17 turn)
- Total throughput: ~60 not/saat max (throttle 60sn ile)

## Güvenlik Modeli

### Permission Gates

1. **autonomous-claude-settings.json allowlist** — claude -p sadece izinli komutları çalıştırabilir
2. **--dangerously-skip-permissions YOK** — bypass disabled
3. **Lock file** — concurrent autonomous spawn engelli
4. **Throttle** — saniyede 1'den fazla spawn olmaz

### Confidence-based Defer

LOW confidence + non-ACK → otomatik DISCUSSION (defer) → kullanıcı görür.

### TEK-NOTE Disiplini

Guardrails markdown'da: "SessionStart hook'la inject edilen diğer notlara dokunma. Sadece prompt'taki note ID için iş yap."

### Yıkıcı işlem yasakları (guardrails + settings deny)

- sudo, systemctl, docker, ssh, scp, rsync
- rm, dd (recursive force delete)
- git push, git rebase, git history rewrite
- gh pr merge/close
- vps-run.sh (production write)
- WebFetch, WebSearch

## Observability

- `/opt/linux-ai-server/data/hook-logs/autonomous-claude.log` — main log
- `/opt/linux-ai-server/data/hook-logs/autonomous-claude-spawn-<id>-<ts>.log` — per-spawn detail
- `/opt/linux-ai-server/data/hook-logs/note-poller.log` — daemon
- `/opt/linux-ai-server/data/hook-logs/autonomous-health.log` — health check
- `/opt/linux-ai-server/data/hook-logs/autonomous-daily-summary.log` — daily summary cron
- Memory entries `autonomous-{ack,deferred,urgent,daily-summary,health-fail}-*` — kullanıcı SessionStart'ta görür

## Failure Modes ve Recovery

| Fail | Detection | Recovery |
|---|---|---|
| Ollama down | health-check FAIL ollama-api | ollama serve restart |
| Lock file orphan | health-check FAIL lock-file-orphan | manual rm /tmp/klipper-autonomous-claude.lock |
| Memory API down | health-check FAIL memory-api | systemctl restart linux-ai-server |
| Note-poller crash | health-check FAIL note-poller-running | systemctl restart klipper-note-poller |
| Disk full | health-check FAIL disk-space | log rotation veya manual cleanup |
| Throttle malformed | health-check FAIL throttle-malformed | manual truncate state file |
| Claude spawn rc!=0 | spawn log'da api_error_status | next iteration retry (poller doğal) |

## Açık Gelişme Yönü

1. **Ollama summarizer tier** — memory entries için local generation (Claude turn azaltır)
2. **Multi-LLM cascade** — ACTIONABLE first try qwen2.5-coder, escalate to Claude
3. **Per-note dedup** — aynı içerik 5dk'da tekrar gelirse skip
4. **Priority queue** — URGENT önce, sonra ACTIONABLE, sonra diğer
5. **Webhook push** — polling yerine event-driven (DB trigger → FastAPI WebSocket)
6. **Surer parity** — Ollama tier sürer-side de PowerShell'den çağrılabilir (`/api/v1/classify/note` endpoint açık, kullanıma sokulmuş)

## Operasyon Komutları

```bash
# Service status
sudo systemctl status klipper-note-poller

# Service logs (live)
sudo journalctl -u klipper-note-poller -f

# Manuel autonomous trigger (test)
bash /opt/linux-ai-server/automation/autonomous-claude.sh <NOTE_ID> "<FROM>" "<TITLE>" "<PREVIEW>"

# Daily summary manuel
bash /opt/linux-ai-server/automation/autonomous-daily-summary.sh

# Health check manuel
bash /opt/linux-ai-server/automation/autonomous-health-check.sh

# Throttle reset (sonraki spawn hemen oluştur)
truncate -s 0 /opt/linux-ai-server/data/hook-state/autonomous-last-spawn.txt

# Bütün autonomous'ı durdur
sudo systemctl stop klipper-note-poller && sudo systemctl disable klipper-note-poller
```
