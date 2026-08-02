# Linux-AI Server — Klipper Sunucu

## Sunucu Bilgileri

> **Bu bolumun kurali — GOZLEM vs KARAR.** Bir komutun uretebildigi hicbir sey burada yazmaz (bayatlar, ve yalanci-kesinlik uretir); yalnizca olculemeyen sey yazar: kararlar, gerekceleri, invaryantlar ve ogrenilmis tuzaklar. **Tek istisna (bootstrap):** kanonik kaynaga ULASMAK icin gereken bilgi, olculebilir olsa da burada kalir — yoksa dosya kendi kendine yetmez.

**Bootstrap — baglanmak icin gerekli, bu yuzden burada:**
- **Hostname:** klipper · **Kullanici:** klipperos (sudo NOPASSWD)
- **Ag:** LAN 192.168.1.113 | Tailscale 100.84.251.49 (klipper-2 olarak kayitli)
- **Servis portu:** 8420 · **Python venv:** `/opt/linux-ai-server/venv`
- **Donanim (degismez):** Beelink SER8 (AZW), AMD Ryzen 7 8845HS w/ Radeon 780M (8C/16T), 28GB RAM, 2 NVMe (Lexar NM790 2TB + Crucial P3 1TB). BIOS V035 P8C0M0C15.14 (26/06/2025)

**Canli durum — DOSYAYA BAKMA, OLC:**

| Ne | Komut |
|---|---|
| Disk/LVM yerlesimi, hangi LV hangi fiziksel diskte | `lsblk -e7 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL` |
| Bos alan | `vgs` (VG bazinda) · `df -h` (fs bazinda) |
| Kernel surumu / OS | `uname -r` · `lsb_release -d` |
| PCIe link durumu | `sudo lspci -vv -s 0000:05:00.0 \| grep LnkSta` (Lexar) |
| NVMe hatalari | `journalctl -k -b 0 \| grep -iE 'genctr mismatch\|invalid id completed'` |

**KARARLAR ve INVARYANTLAR** (olculemez — kaynagi yalniz bu dosya):
- **Disk rol ayrimi (2026-08-01 karari):** Lexar = yalniz yedekleme/soguk veri · Crucial = aktif her sey. Gerekce: Lexar'in PCIe linki kararsiz (kesif 1479). **Aktif veriyi Lexar'a koyma.**
- Tum mountlar **UUID-tabanli** → slot degisimi guvenli. Yeni mount eklerken bunu bozma.
- Lexar'da mount-suz duran `lv-models` ve eski `ubuntu-lv` **bilerek** bekletiliyor (ollama ve Faz C rollback'i); dogrulaninca silinip VG'ye iade edilecek. Bos gorunuyorlar diye silme.
- `fstab` duzenledikten sonra **`systemctl daemon-reload` sart** — atlanirsa bayat mount unit eski cihaza baglanir ve yeni mount sessizce dusebilir.

**ACIK PROBLEM — kesif 1479 (Lexar PCIe link kararsizligi):**
- Belirti: boot'ta 16GT/s x4 egitiyor, bir sure sonra `genctr mismatch` / `invalid id completed` uretip **2.5GT/s'e dusup orada kaliyor**. Crucial ayni surede 0 hata.
- Teshis: diskin PHY/sinyal-butunlugu arizasi — Gen4'te hata veriyor, Gen1 kararli geri-cekilmesi.
- **Veri riski yok:** fs/blok I/O hatasi hic gorulmedi, SMART temiz, kapasite dogrulandi → **sahte kapasite DEGIL**.
- Ayri bir bulgu: 4K-rastgelede Crucial'in ~2.5x gerisinde. Bu **linkten degil**, DRAM-less diskin kendisinden (Gen4'teyken de olculdu) — link duzelse de gecmez.
- **KAPATMA KRITERI:** yuk altinda **~25 saat kesintisiz hatasiz** sure. Daha kisa temiz pencereler kanit degil — taban hiz ~0.12 hata/saat oldugu icin birkac saatlik sessizlik hicbir sey degismese de beklenen sonuctur.

**OGRENILMIS TUZAKLAR** (bunlari bilmeden olcen yanlis sonuca varir):
- **Link durumunu boot'tan hemen sonra kontrol etme** — yuk + zaman gectikten sonra bak. Boot'ta saglikli gorunmesi hicbir sey soylemez.
- **NVMe hatasi sayarken `dmesg` KULLANMA** — halka tampon doluyor ve boot'un ilk saatlerini sessizce dusuruyor, sahte "0 hata" uretiyor (2026-08-02'de dmesg'de hic nvme satiri kalmamisti). `journalctl -k -b 0` kullan (onceki boot: `-b -1`).
- **Sogutucu sokup yeniden oturtmak link sorununu DUZELTMEZ**, yalnizca hata sayacini sifirlar — "duzeldi" gibi gorunur.
- Ollama'yi Crucial'a tasimak soguk model yuklemesini ~2x hizlandirdi ama **token uretimini degistirmedi** — uretim %100 CPU-bound, disk-disi. Disk degisikligiyle tok/s beklemeyin.

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
- Ollama host'ta (11434) — modeller: qwen3:30b-a3b-instruct-2507-q3_K_M (RAG default + reasoning, thinking-siz MoE — q4_K_M'den 2026-07-23'te değiştirildi: TR-eval C+D bloğu 20/20 ile birebir aynı kalite + ~1.9x daha hızlı (8.1s vs 15.4s/soru ort.), tuzak-soru/halüsinasyon testi #19 dahil geçti; "default" etiketi fiktif, gerçek-tüketici bağlanana kadar açık-soru, topic-5 kararı), qwen3.5:9b (classify — 2026-07-12'de 2507'den taşındı), qwen2.5:3b (consciousness deep-thought), gemma3:12b-it-qat (TR-hi, GEÇİCİ — rolü DeepSeek-Layer2'ye taşınacak, topic-5 kararı P2/surer-lane tamamlanınca kaldırılır), qwen2.5:7b (KISMİ-EMEKLİ, 2026-07-24: automation-lane'in 4 script'i (autonomous-health-check.sh, autonomous-classifier-v2.sh, autonomous-spawn-summarize.sh, signal_quality.py) qwen3.5:9b'e taşındı + `think:false` eklendi (qwen3.5 hibrit-thinking, atlanırsa "response" boş kalır — LLMCore-DIŞI direkt-Ollama çağrıları bu korumanın dışında). dispatch.py qwen2.5:7b'DE KALDI: canlı-test KLIPPER/SURER/HYBRID route+komut-analizinde qwen3.5:9b tutarsız/güvenilmez çıktı verdi (route hep "HYBRID", komut uydurma, sahte surer_tasks) — qwen2.5:7b bu yüzden DİSKTE KALICI, "5→4 script" değil; LLMCore-DIŞI doğrudan Ollama çağrısı yapıyor, bu yüzden llm_calls telemetrisinde görünmüyor), bge-m3 + nomic-embed-text (embed). qwen3-coder:30b SİLİNDİ (2026-07-19, topic-5 kararı — 0-çağrı kanıtlı, .env LLM_ROUTE_CODE_REVIEW override'i kalıcı, code-review fiilen her zaman claude-haiku'ya gidiyordu; 4-model-önerisi qwen3.6:27b/r1:32b/r1:8b/openthinker:7b de aynı kararla reddedildi). Hibrit-thinking modeller (qwen3.5/3.6 ailesi) için `think:false` llmcore.py'de koşulsuz uygulanır (allowlist yok, güvenli-varsayılan)

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
- `/var/log/` — **ayri LV** (`vg-storage/lv-log`, 98G, nvme1n1 uzerinde; 2026-08-01'de root'tan tasindi). Eski icerik mount altinda gizli duruyor (471M, rollback).
- `/var/log/linux-ai-server/` — Cron job stdout/stderr (klipper-cron-wrap.sh per-job log). Append, rotate yok.
- `/opt/linux-ai-server/logs/` — Test runner gunluk rotated log (`test-runner-YYYYMMDD.log`) + fail snapshots (`test-fail-*`) + artifact dirs (`e2e/`)
- `/opt/linux-ai-server/data/` — Database files (server.db, claude_memory.db, coverage.db) + autonomous spawn logs (`hook-logs/`) + lock/hook state (`hook-state/`). **Mod `2775` olmali — sticky bit (`+t`) KOYMA.** Bu DB'lere hem `klipperos` (API) hem `klipper-auto` (cron) yaziyor; `fs.protected_regular=2` ile sticky+grup-yazilabilir dizinde SQLite `-wal`/`-shm` dosyalarini sahibi olmayan kullanici `O_CREAT` ile acamaz → tum yazmalar `attempt to write a readonly database` ile 500 doner (2026-08-02'de 90dk kesinti, disc#1486)
- `/opt/linux-ai-server/data/klipper-event.log` — klipper-event.sh systemd/cron event log

## Komutlar
sudo systemctl restart linux-ai-server
journalctl -u linux-ai-server -f
docker ps -a
# Kernel modul gelistirme (gecici test): cd kernel && make && sudo insmod proc_linux_ai.ko
# Kalici/kernel-upgrade-guvenli (DKMS, .c degisince): bash kernel/install-dkms.sh

## License
Apache-2.0 — see `LICENSE` at repo root.
