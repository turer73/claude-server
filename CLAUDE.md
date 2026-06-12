# Linux-AI Server — Klipper Sunucu

## Sunucu Bilgileri
- **Hostname:** klipper
- **Donanim:** Beelink SER8 (AZW) — BIOS V035 P8C0M0C15.14 (26/06/2025)
- **OS:** Ubuntu 26.04 LTS (Resolute)
- **Kernel:** 7.0.0-22-generic (canli, reboot 2026-06-11) + 3 ozel modul (proc_linux_ai, nf_linux_ai, usb_linux_ai) — **DKMS-yonetimli**, kernel-upgrade'de otomatik rebuild
- **CPU:** AMD Ryzen 7 8845HS w/ Radeon 780M, 8 cekirdek / 16 thread
- **RAM:** 28GB (27946896 kB)
- **Disk:** 98GB SSD (LVM), 28GB kullanildi
- **Ag:** LAN 192.168.1.113 | Tailscale 100.84.251.49 (klipper-2 olarak kayitli)
- **Python:** 3.14 (venv: /opt/linux-ai-server/venv)
- **Kullanici:** klipperos (sudo NOPASSWD)

## Servis
- **Port:** 8420
- **Framework:** FastAPI + Uvicorn (2 worker)
- **DB:** 4 SQLite — server.db (ana, alerts/audit_log/metrics + vps_metrics_history), claude_memory.db (hafiza/sessions/tasks_log), coverage.db (test trend + CI/test sonuclari, gunluk run-all-tests.sh), rag_metrics.db (RAG). (ci_tests.db 2026-06-01'de retire edildi — otomasyon hic yazmamisti, olu kod; arsiv data/ci_tests.db.gz)
- **Auth:** JWT + API Key
- **Systemd:** linux-ai-server.service
- **Endpoint sayisi:** 161 (OpenAPI'den, 2026-06-12)

## Proje Yapisi
- app/api/ — 37 route dosyasi (admin, agents, ai, auth, backup, ci, classifier, claude_code, csp, deploy, dev, devops, digest, files, kernel, llm, logs, memory, monitoring, n8n, network, projects, prometheus, rag, research, shell, social, ssh, system, tasks, telegram_bot, validation, vps, webhooks, webops, ws_status)
- app/core/ — Is mantigi (shell_executor, terminal_manager, ai_inference, task_queue, devops_agent)
- app/auth/ — JWT + API key
- app/mcp/ — MCP server (Claude entegrasyonu)
- app/ws/ — WebSocket (terminal, monitor, logs)
- app/dashboard/ — Super Dashboard v2 (xterm.js terminal)
- app/claude_ui/ — Claude Code web chat
- kernel/ — 3 ozel C kernel modulu (proc, netfilter, usb)
- automation/ — Cron scriptleri
- tests/ — 94 dosya / 1128 test (pytest, asyncio_mode=auto; sayim 2026-06-12)

## API Endpointleri
- /dashboard — Super Dashboard v2
- /claude — Claude Code chat arayuzu
- /api/v1/shell/exec — Komut calistir
- /api/v1/kernel/* — Kernel kontrol
- /api/v1/system/* — Sistem yonetimi
- /api/v1/files/* — Dosya islemleri
- /api/v1/monitor/* — Metrikler
- /api/v1/claude/* — Claude Code API (run, stream, status)
- /api/v1/vps/* — VPS yonetimi
- /api/v1/devops/* — DevOps agent
- /ws/terminal — WebSocket terminal (PTY)

## Docker Konteynerler (9 aktif)
- **Gozlem:** dozzle (9999), uptime-kuma (3001), grafana (3030), prometheus (9090), node-exporter (9100), cadvisor (9080)
- **Otomasyon/RAG:** n8n (5678), qdrant (6333/6334)
- **Arac:** stirling-pdf (8090)
- Ollama host'ta (11434) — modeller: qwen2.5:3b (default LLM), qwen2.5:7b, qwen2.5-coder:7b, aya:8b (TR-hi), bge-m3 + nomic-embed-text (embed)

VPS Dokploy uzerinde ayrica baska servisler var (asagi bkz).

## Kernel Modulleri
- proc_linux_ai — /proc/linux_ai (CPU, RAM, uptime, esikler)
- nf_linux_ai — /proc/linux_ai_firewall (IP engelleme)
- usb_linux_ai — /proc/linux_ai_usb (USB whitelist)

**DKMS:** Moduller DKMS'e bagli (`linux-ai/1.0`) — her kernel upgrade'inde otomatik rebuild+install (`/etc/kernel/postinst.d/dkms` hook), boot'ta `/etc/modules-load.d/linux-ai.conf` ile yuklenir. Kaynak=git (`kernel/*.c`), DKMS kopyasi=`/usr/src/linux-ai-1.0`. Kayit/yeniden-kayit: `bash kernel/install-dkms.sh` (idempotent; DKMS kaydi + boot autoload dosyasini `kernel/modules-load.conf`'tan kurar). Durum: `dkms status linux-ai`.

## Iliskili Projeler

### PetVet (petvet.panola.app)
Veteriner + pet shop yonetimi. React 19, Cloudflare Workers + D1. 64 test.
GitHub: github.com/turer73/petvet
Test: REDACTED_PHONE / test1234

### Kuafor SaaS (kuafor.panola.app)
Salon yonetimi. React 19, Cloudflare Workers + D1. 50 test.
GitHub: github.com/turer73/kuafor
Test: REDACTED_PHONE / test1234

### Panola ERP (panola.app)
Siparis/uretim/stok/CRM. React 19, Supabase. 898 test.

### Linux-AI Server (100.84.251.49:8420)
Bu sunucu. FastAPI, kernel modulleri, 94 test dosyasi.
GitHub: github.com/turer73/claude-server

## VPS (Contabo) — 20 konteyner (audit: 2026-06-01, surer doğrulanmış)
Dokploy v0.29.2 + Traefik v3.1 (root reverse proxy 80/443). /api/v1/vps/exec (SSH) ile yonetim.

**Klipper-first hedefi: 5 servis tasinmis** (n8n + grafana + prometheus + cadvisor + uptime-kuma). Dashy VPS'te kaldi (asagi bkz).

**VPS'te kalan production (public domain gerekligi):**
- panola.app: caddy + gotrue + postgres + postgrest (4 container)
- bilge-english: app(Next.js) + auth + postgres + postgrest + realtime (5 container)
- bilge-arena: postgrest + realtime (2 container, data layer)
- plausible analytics: app + postgres + clickhouse (3 container)
- csp-collector (csp.3d-labx.com), social-media-server (media.3d-labx.com)
- dokploy stack: dokploy + postgres + redis + traefik (4 container)
- node-exporter (VPS-side host metrics)

**Bilincli VPS-only bırakılan:** dashy (~858MB, internal dashboard, ROI sifir).

**Detay/migration plani:** memory `architecture-vps-klipper-migration-2026-05-26`

## Cloudflare
Hesap: REDACTED_EMAIL
Workers: kuafor-api, petvet-api
Pages: panola, kuafor-panel, petvet-panel
D1: kuafor-db, petvet-db

## Hafiza Sistemi (Merkezi SQLite)
- **DB:** /opt/linux-ai-server/data/claude_memory.db
- **API:** /api/v1/memory/* (X-Memory-Key header gerekli)
- **Helper:** bash /opt/linux-ai-server/scripts/claude-memory.sh
- **Skill:** /memory — dashboard, save, bug, fix, note, search, sessions, tasks
- **SessionStart hook:** scripts/hooks/session-start.sh (settings.json uzerinden otomatik) — acik bug, okunmamis not, son oturum, son test sonucu inject eder
- **Cihazlar:** klipper (linux), windows-masaustu, windows-laptop, android-telefon
- **Tablolar:** memories, sessions, tasks_log, discoveries, notes, devices, device_projects, command_log

Oturum basinda hook DB durumunu otomatik yukler. Her oturum sonunda /memory save ile oturumu kaydet.

## Log Dizinleri (amac ayrimi)
- `/var/log/linux-ai-server/` — Cron job stdout/stderr (klipper-cron-wrap.sh per-job log). Append, rotate yok.
- `/opt/linux-ai-server/logs/` — Test runner gunluk rotated log (`test-runner-YYYYMMDD.log`) + fail snapshots (`test-fail-*`) + artifact dirs (`e2e/`)
- `/opt/linux-ai-server/data/` — Database files (server.db, claude_memory.db, coverage.db) + autonomous spawn logs (`hook-logs/`) + lock/hook state (`hook-state/`)
- `/opt/linux-ai-server/data/klipper-event.log` — klipper-event.sh systemd/cron event log

## Komutlar
sudo systemctl restart linux-ai-server
journalctl -u linux-ai-server -f
docker ps -a
# Kernel modul gelistirme (gecici test): cd kernel && make && sudo insmod proc_linux_ai.ko
# Kalici/kernel-upgrade-guvenli (DKMS, .c degisince): bash kernel/install-dkms.sh

## License
Apache-2.0 — see `LICENSE` at repo root.
