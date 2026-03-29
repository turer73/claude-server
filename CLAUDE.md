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
- `app/api/` — Route handlers (shell, files, system, kernel, ssh, ai, claude_code, vps, deploy, tasks, monitoring, logs, devops, rag)
- `app/core/` — Is mantigi (shell_executor, terminal_manager, ai_inference, task_queue, devops_agent)
- `app/auth/` — JWT + API key
- `app/mcp/` — MCP server (Claude entegrasyonu)
- `app/ws/` — WebSocket (terminal, monitor, logs)
- `app/dashboard/` — Super Dashboard v2 (xterm.js terminal)
- `app/claude_ui/` — Claude Code web chat
- `kernel/` — 3 ozel C kernel modulu (proc, netfilter, usb)
- `automation/` — Cron scriptleri
- `tests/` — 430+ pytest testi

## API Endpointleri
- `/dashboard` — Super Dashboard v2
- `/claude` — Claude Code chat arayuzu
- `/api/v1/shell/exec` — Komut calistir
- `/api/v1/kernel/*` — Kernel kontrol
- `/api/v1/system/*` — Sistem yonetimi
- `/api/v1/files/*` — Dosya islemleri
- `/api/v1/monitor/*` — Metrikler
- `/api/v1/claude/*` — Claude Code API (run, stream, status)
- `/api/v1/vps/*` — VPS yonetimi
- `/api/v1/devops/*` — DevOps agent
- `/ws/terminal` — WebSocket terminal (PTY)

## Docker Konteynerler
Coolify, Gitea, Paperless-ngx, Grafana, Prometheus, n8n, ChromaDB

## Kernel Modulleri
- `proc_linux_ai` — /proc/linux_ai (CPU, RAM, uptime, esikler)
- `nf_linux_ai` — /proc/linux_ai_firewall (IP engelleme)
- `usb_linux_ai` — /proc/linux_ai_usb (USB whitelist)

## Iliskili Projeler

### PetVet (petvet.panola.app)
Veteriner + pet shop yonetimi. React 19, Cloudflare Workers + D1. 64 test.
GitHub: github.com/turer73/petvet | Test: REDACTED_PHONE / test1234

### Kuafor SaaS (kuafor.panola.app)
Salon yonetimi. React 19, Cloudflare Workers + D1. 50 test.
GitHub: github.com/turer73/kuafor | Test: REDACTED_PHONE / test1234

### Panola ERP (panola.app)
Siparis/uretim/stok/CRM. React 19, Supabase. 898 test.

## VPS (Contabo)
Coolify, Uptime Kuma, n8n, Plausible. /api/v1/vps/exec ile yonetim.

## Cloudflare
Hesap: REDACTED_EMAIL
Workers: kuafor-api, petvet-api
Pages: panola, kuafor-panel, petvet-panel
D1: kuafor-db, petvet-db

## Komutlar
```bash
# Test
cd /opt/linux-ai-server && source venv/bin/activate && python -m pytest tests/ -q

# Restart
sudo systemctl restart linux-ai-server

# Loglar
journalctl -u linux-ai-server -f

# Kernel modul
cd /opt/linux-ai-server/kernel && make && sudo insmod proc_linux_ai.ko

# Docker
docker ps -a
```
