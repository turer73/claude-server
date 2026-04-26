# Linux-AI Server — Klipper Sunucu

## Sunucu Bilgileri
- **Hostname:** klipper
- **OS:** Ubuntu 24.04.4 LTS (Noble Numbat)
- **Kernel:** 6.8.0-101-generic + 3 ozel modul (proc_linux_ai, nf_linux_ai, usb_linux_ai)
- **CPU:** Intel i5, 4 cekirdek
- **RAM:** 8GB DDR4
- **Disk:** 108GB SSD, 34GB kullanildi
- **Ag:** LAN REDACTED_LAN_IP | Tailscale REDACTED_TAILSCALE_IP
- **Kullanici:** klipperos (sudo NOPASSWD)

## Servis
- **Port:** 8420
- **Framework:** FastAPI + Uvicorn (2 worker)
- **DB:** SQLite (/opt/linux-ai-server/data/server.db)
- **Auth:** JWT + API Key
- **Systemd:** linux-ai-server.service

## Proje Yapisi
- app/api/ — Route handlers (shell, files, system, kernel, ssh, ai, claude_code, vps, deploy, tasks, monitoring, logs, devops, rag)
- app/core/ — Is mantigi (shell_executor, terminal_manager, ai_inference, task_queue, devops_agent)
- app/auth/ — JWT + API key
- app/mcp/ — MCP server (Claude entegrasyonu)
- app/ws/ — WebSocket (terminal, monitor, logs)
- app/dashboard/ — Super Dashboard v2 (xterm.js terminal)
- app/claude_ui/ — Claude Code web chat
- kernel/ — 3 ozel C kernel modulu (proc, netfilter, usb)
- automation/ — Cron scriptleri
- tests/ — 430+ pytest testi

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

## Docker Konteynerler
Coolify, Gitea, Paperless-ngx, Grafana, Prometheus, n8n, ChromaDB

## Kernel Modulleri
- proc_linux_ai — /proc/linux_ai (CPU, RAM, uptime, esikler)
- nf_linux_ai — /proc/linux_ai_firewall (IP engelleme)
- usb_linux_ai — /proc/linux_ai_usb (USB whitelist)

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

### Linux-AI Server (REDACTED_TAILSCALE_IP:8420)
Bu sunucu. FastAPI, kernel modulleri, 430 test.
GitHub: github.com/turer73/claude-server

## VPS (Contabo)
Coolify, Uptime Kuma, n8n, Plausible. /api/v1/vps/exec ile yonetim.

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
- **Oturum basi:** bash /opt/linux-ai-server/scripts/memory-session-start.sh
- **Cihazlar:** klipper (linux), windows-masaustu, windows-laptop, android-telefon
- **Tablolar:** memories, sessions, tasks_log, discoveries, notes, devices, device_projects, command_log

Her oturum basinda `memory-session-start.sh` calistir, acik buglari ve okunmamis notlari kontrol et.
Her oturum sonunda /memory save ile oturumu kaydet.

## Yari Otonom Hook Sistemi (scripts/hooks/)
Claude Code icin SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop hook'lari.
Kurulum: `bash /opt/linux-ai-server/scripts/hooks/install.sh` (~/.claude/settings.json'a yazar).
Kontrol: `bash /opt/linux-ai-server/scripts/hooks/install.sh --check`.
Loglar: /opt/linux-ai-server/data/hook-logs/

Hook'lar:
- **session-start.sh** — oturum acilinca hafiza durumu, acik bug, plan, notlari context'e enjekte eder
- **user-prompt-log.sh** — her prompt'u TSV'ye kaydeder (user-prompts.tsv) — rationale audit
- **pre-bash-guard.sh** — yikici komutlari (rm -rf, force push, DROP TABLE, vb.) BLOKLAR; otonom modda HOOK_DESTRUCTIVE_ACK=1 ile bypass
- **post-bash-capture.sh** — pytest/npm/tsc/git commit gibi komutlarin ciktisini yakalar; FAIL ise discoveries'e bug yazar (last-test-results.tsv)
- **stop-save-session.py** — oturum bitince transcript'i ozetleyip Memory API'ye session kaydeder

Otonomi modlari (env HOOK_AUTONOMY):
- `supervised` (default) — yikici komutlar engellenir, kullanici onayi sart
- `autonomous` — HOOK_DESTRUCTIVE_ACK=1 ile guard atlanabilir (UI/CI icinde)

## Komutlar
sudo systemctl restart linux-ai-server
journalctl -u linux-ai-server -f
docker ps -a
cd /opt/linux-ai-server/kernel && make && sudo insmod proc_linux_ai.ko
