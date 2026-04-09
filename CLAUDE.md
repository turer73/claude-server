# Linux-AI Server — Klipper Sunucu

## Sunucu Bilgileri
- **OS:** Ubuntu 24.04 LTS
- **Kernel:** 6.8.0 + 3 ozel modul (proc_linux_ai, nf_linux_ai, usb_linux_ai)
- **Ag:** .env dosyasinda tanimli (LAN_IP, TAILSCALE_IP)

## Servis
- **Framework:** FastAPI + Uvicorn (2 worker)
- **DB:** SQLite (yol .env'de tanimli)
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
Test credential'lari .env dosyasinda.

### Kuafor SaaS (kuafor.panola.app)
Salon yonetimi. React 19, Cloudflare Workers + D1. 50 test.
Test credential'lari .env dosyasinda.

### Panola ERP (panola.app)
Siparis/uretim/stok/CRM. React 19, Supabase. 898 test.

### Linux-AI Server
Bu sunucu. FastAPI, kernel modulleri, 430 test.

## VPS (Contabo)
Coolify, Uptime Kuma, n8n, Plausible. /api/v1/vps/exec ile yonetim.
VPS erisim bilgileri .env dosyasinda (VPS_HOST).

## Cloudflare
Workers: kuafor-api, petvet-api
Pages: panola, kuafor-panel, petvet-panel
D1: kuafor-db, petvet-db
Hesap bilgileri .env dosyasinda.

## Hafiza Sistemi (Merkezi SQLite)
- **API:** /api/v1/memory/* (X-Memory-Key header gerekli)
- **Skill:** /memory — dashboard, save, bug, fix, note, search, sessions, tasks
- **Tablolar:** memories, sessions, tasks_log, discoveries, notes, devices, device_projects, command_log

Her oturum basinda `memory-session-start.sh` calistir, acik buglari ve okunmamis notlari kontrol et.
Her oturum sonunda /memory save ile oturumu kaydet.

## Komutlar
sudo systemctl restart linux-ai-server
journalctl -u linux-ai-server -f
docker ps -a
