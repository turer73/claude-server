# Linux-AI Server — Klipper Sunucu

## Sunucu Bilgileri
- **Hostname:** klipper
- **Donanim:** Beelink SER8 (AZW) — BIOS V035 P8C0M0C15.14 (26/06/2025)
- **OS:** Ubuntu 26.04 LTS (Resolute)
- **Kernel:** 7.0.0-15-generic + 3 ozel modul (proc_linux_ai, nf_linux_ai, usb_linux_ai)
- **CPU:** AMD Ryzen 7 8845HS w/ Radeon 780M, 8 cekirdek / 16 thread
- **RAM:** 28GB (27946896 kB)
- **Disk:** 98GB SSD (LVM), 28GB kullanildi
- **Ag:** LAN 192.168.1.113 | Tailscale 100.84.251.49 (klipper-2 olarak kayitli)
- **Python:** 3.14 (venv: /opt/linux-ai-server/venv)
- **Kullanici:** klipperos (sudo NOPASSWD)

## Servis
- **Port:** 8420
- **Framework:** FastAPI + Uvicorn (2 worker)
- **DB:** SQLite (/opt/linux-ai-server/data/server.db)
- **Auth:** JWT + API Key
- **Systemd:** linux-ai-server.service

## Proje Yapisi
- app/api/ — 31 route dosyasi (shell, files, system, kernel, ssh, ai, claude_code, vps, deploy, tasks, monitoring, logs, devops, rag, agents, ci, backup, csp, dev, digest, network, projects, prometheus, social, validation, webhooks, webops, ws_status, memory, auth)
- app/core/ — Is mantigi (shell_executor, terminal_manager, ai_inference, task_queue, devops_agent)
- app/auth/ — JWT + API key
- app/mcp/ — MCP server (Claude entegrasyonu)
- app/ws/ — WebSocket (terminal, monitor, logs)
- app/dashboard/ — Super Dashboard v2 (xterm.js terminal)
- app/claude_ui/ — Claude Code web chat
- kernel/ — 3 ozel C kernel modulu (proc, netfilter, usb)
- automation/ — Cron scriptleri
- tests/ — 61 test dosyasi (pytest)

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
- Ollama host'ta (11434) — modeller: bge-m3 (embed), qwen2.5:7b (LLM)

VPS Dokploy uzerinde ayrica baska servisler var (asagi bkz).

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

### Linux-AI Server (100.84.251.49:8420)
Bu sunucu. FastAPI, kernel modulleri, 61 test dosyasi.
GitHub: github.com/turer73/claude-server

## VPS (Contabo)
Dokploy v0.28.8 + Traefik v3.1 orchestrator, Caddy 2 reverse proxy. Servisler: Plausible, n8n, OpenClaw, Panola (Postgres+PostgREST+Auth), Grafana+Prometheus+cAdvisor, Dashy, Uptime Kuma. 21 konteyner aktif. /api/v1/vps/exec ile yonetim.

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

## Komutlar
sudo systemctl restart linux-ai-server
journalctl -u linux-ai-server -f
docker ps -a
cd /opt/linux-ai-server/kernel && make && sudo insmod proc_linux_ai.ko
