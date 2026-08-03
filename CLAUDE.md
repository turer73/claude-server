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
- Teshis **acik, iki hipotez**: (a) diskin PHY/sinyal-butunlugu arizasi, (b) IOMMU/DMA — hatanin hemen oncesinde **ayni saniyede** `AMD-Vi: Event logged [IO_PAGE_FAULT]` var (2026-08-03 05:00:50, n=1). Ayirt edici gozlem: bir sonraki dususte IO_PAGE_FAULT yine once mi geliyor. **"PHY kesin" diye yazmayin.**
- **Veri riski yok:** fs/blok I/O hatasi hic gorulmedi, SMART temiz, kapasite dogrulandi → **sahte kapasite DEGIL**.
- Ayri bir bulgu: 4K-rastgelede Crucial'in ~2.5x gerisinde. Bu **linkten degil**, DRAM-less diskin kendisinden (Gen4'teyken de olculdu) — link duzelse de gecmez.
- **KAPATMA KRITERI (iki kosul, BIRLIKTE):** yuk altinda **~25 saat hatasiz** *ve* **link hala 16.0 GT/s**. Hata sayaci tek basina GECERSIZ: disk 2.5GT/s'e dustukten sonra hata **uretmiyor** — 2026-08-03 05:00:50'deki dusustan sonraki ~13 saat tamamen temizdi. **Sessizlik saglik degil, teslim olabilir**; her hata-kontrolunun yaninda `current_link_speed` de okunur.

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

## Proje Yapisi

Dizin **sayilari ve dosya listeleri buraya yazilmaz** (`ls app/api/*.py | wc -l`, `find tests -name '*.py' | wc -l` uretir). Yalnizca her dizinin NE ISE YARADIGI yazar — o uretilemez:

- `app/api/` — HTTP route'lari (dosya basina bir alan: memory, shell, vps, kernel, rag, ...)
- `app/core/` — Is mantigi (shell_executor, terminal_manager, ai_inference, task_queue, devops_agent)
- `app/auth/` — JWT + API key
- `app/mcp/` — MCP server (Claude entegrasyonu)
- `app/ws/` — WebSocket (terminal, monitor, logs)
- `app/dashboard/` — Super Dashboard v2 (xterm.js terminal)
- `app/claude_ui/` — Claude Code web chat
- `kernel/` — 3 ozel C kernel modulu (proc, netfilter, usb)
- `automation/` — Cron scriptleri
- `tests/` — pytest, `asyncio_mode=auto`

## API Endpointleri

**Tam liste buraya yazilmaz** — kanonik kaynak OpenAPI, bayatlamaz:
`curl -s http://127.0.0.1:8420/openapi.json | python3 -c "import json,sys; print('\n'.join(sorted(json.load(sys.stdin)['paths'])))"`

Bootstrap istisnasi — gunluk kullanilan giris noktalari:
- `/dashboard` — Super Dashboard v2 · `/claude` — Claude Code chat
- `/api/v1/memory/*` — hafiza · `/api/v1/shell/exec` — komut calistir
- `/api/v1/vps/exec` — VPS'e SSH · `/ws/terminal` — WebSocket PTY

**Auth (tek kapi — `require_auth`, `app/middleware/dependencies.py`):** korumali her route **ucunden birini** kabul eder — `X-API-Key` (ic otomasyon: n8n/cron/webhook), `X-Memory-Key` (hafiza istemcileri: surer, klipper-autonomous), veya Bearer JWT. **Ilk ikisi `admin` kapsami verir**, yani `require_admin`'li route'lar (ornegin `shell/exec`) `X-Memory-Key` ile de calisir — "shell/exec JWT ister, X-Memory-Key olmaz" DOGRU DEGIL.

## Docker Konteynerler

**Konteyner listesi ve portlar buraya yazilmaz** — `docker ps` uretir. Burada yalnizca KARAR durur:

- **Klipper-first:** gozlem/otomasyon stack'i (n8n, grafana, prometheus, cadvisor, uptime-kuma) VPS'ten klipper'a tasindi.
- **Dashy bilerek VPS'te birakildi** — internal dashboard, tasima ROI'si sifir. Tekrar onerilmesin.

## Ollama / Model Rolleri

Ollama host'ta (11434). **Kurulu model listesi buraya yazilmaz** (`ollama list` uretir) — burada yalnizca ROL ATAMALARI ve gerekceleri durur, cunku onlar karardir:

- **`qwen3:30b-a3b-instruct-2507-q3_K_M`** — RAG default + reasoning (thinking-siz MoE). 2026-07-23'te q4_K_M'den q3'e dusuruldu: TR-eval C+D blogu 20/20 ile **birebir ayni kalite**, ~1.9x daha hizli (8.1s vs 15.4s/soru ort.), tuzak-soru/halusinasyon testi #19 dahil gecti. "default" etiketi fiktif — gercek-tuketici baglanana kadar acik-soru (topic-5 karari).
- **`qwen3.5:9b`** — classify (2026-07-12'de 2507'den tasindi).
- **`qwen2.5:3b`** — consciousness deep-thought.
- **`gemma3:12b-it-qat`** — TR-hi, **GECICI**: rolu DeepSeek-Layer2'ye tasinacak, topic-5 karari geregi P2/surer-lane tamamlaninca kaldirilir.
- **`qwen2.5:7b`** — **KISMI-EMEKLI**, 2026-07-24: automation-lane'in 4 script'i (`autonomous-health-check.sh`, `autonomous-classifier-v2.sh`, `autonomous-spawn-summarize.sh`, `signal_quality.py`) `qwen3.5:9b`'e tasindi. **Ama `dispatch.py` 7b'DE KALDI ve DISKTE KALICI** — canli testte KLIPPER/SURER/HYBRID route+komut analizinde 9b tutarsiz/guvenilmez cikti verdi (route hep "HYBRID", komut uydurma, sahte surer_tasks). "5→4 script" diye ozetlemeyin; **"tutarlilik icin 9b'ye gecirelim" onerisi bu kanitla reddedildi.** LLMCore-DISI dogrudan Ollama cagrisi yapiyor, bu yuzden `llm_calls` telemetrisinde gorunmuyor.
- **`bge-m3` + `nomic-embed-text`** — embed.
- **`qwen3-coder:30b` SILINDI** (2026-07-19, topic-5): 0-cagri kanitli, `.env` `LLM_ROUTE_CODE_REVIEW` override'i kalici, code-review fiilen her zaman claude-haiku'ya gidiyordu. Ayni kararla 4-model onerisi de (`qwen3.6:27b`/`r1:32b`/`r1:8b`/`openthinker:7b`) reddedildi. **Yeniden onermeyin.**

**Tuzak:** hibrit-thinking modellerde (qwen3.5/3.6 ailesi) `think:false` atlanirsa `response` **bos** doner. `llmcore.py`'de kosulsuz uygulanir (allowlist yok, guvenli-varsayilan) — ama **LLMCore-DISI dogrudan Ollama cagrilari bu korumanin disinda**, orada elle eklemek gerekir.

VPS Dokploy uzerinde ayrica baska servisler var (asagi bkz).

## Kernel Modulleri
- proc_linux_ai — /proc/linux_ai (CPU, RAM, uptime, esikler)
- nf_linux_ai — /proc/linux_ai_firewall (IP engelleme)
- usb_linux_ai — /proc/linux_ai_usb (USB whitelist)

Uc modul adi ve gorevi burada durur — bunlar projenin kendi urettigi bilesenler, drift etmiyorlar. **Calisma durumu/surum buraya yazilmaz:** `lsmod | grep linux_ai` ve `dkms status linux-ai` ile bak.

**DKMS:** Moduller DKMS'e bagli (`linux-ai/1.0`) — her kernel upgrade'inde otomatik rebuild+install (`/etc/kernel/postinst.d/dkms` hook), boot'ta `/etc/modules-load.d/linux-ai.conf` ile yuklenir. Kaynak=git (`kernel/*.c`), DKMS kopyasi=`/usr/src/linux-ai-1.0`. Kayit/yeniden-kayit: `bash kernel/install-dkms.sh` (idempotent; DKMS kaydi + boot autoload dosyasini `kernel/modules-load.conf`'tan kurar).

**Iki yol da GECERLI, amaclari farkli — birbirinin yerine gecmez:**
- Elle `make && sudo insmod` = **gecici gelistirme testi**. `.c`'yi degistirip hizlica denemek icin. Reboot'ta ve kernel yukseltmesinde kaybolur.
- `bash kernel/install-dkms.sh` = **kalici + kernel-upgrade-guvenli**. DKMS elle yolun yerine gecmez, uzerine gelir.

## Iliskili Projeler

### PetVet (petvet.panola.app)
Veteriner + pet shop yonetimi. React 19, Cloudflare Workers + D1.
GitHub: github.com/turer73/petvet
Test: REDACTED_PHONE / test1234

### Kuafor SaaS (kuafor.panola.app)
Salon yonetimi. React 19, Cloudflare Workers + D1.
GitHub: github.com/turer73/kuafor
Test: REDACTED_PHONE / test1234

### Panola ERP (panola.app)
Siparis/uretim/stok/CRM. React 19, Supabase.

### Linux-AI Server (100.84.251.49:8420)
Bu sunucu. FastAPI, kernel modulleri.
GitHub: github.com/turer73/claude-server

## VPS (Contabo)

Dokploy + Traefik (root reverse proxy 80/443). Yonetim `/api/v1/vps/exec` (SSH) uzerinden.

**Konteyner envanteri ve sayisi buraya yazilmaz** — bayatlar ve bunu kimse fark etmez. Canli cek:

```
curl -s -X POST http://127.0.0.1:8420/api/v1/vps/exec \
  -H "X-Memory-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"command":"docker ps --format \"{{.Names}}\t{{.Status}}\" | sort"}'
```

> **Liste tutmanin maliyeti — kanit (2026-08-02/03):** bu bolum "20 konteyner (audit 2026-06-01)" diyordu; **kendi alt-listesi 22'ye topluyordu** (dokuman kendi icinde bile tutarsizdi); canli sayim da 22 ama uyeler farkliydi — `coturn`/`livekit` eklenmisti, `bilge-english-postgrest`/`-realtime` ise yoktu.
> Iki ayri hata sinifi vardi ve **ikincisi daha sinsi**: (1) *drift* — coturn/livekit sonradan eklendi, dokuman guncellenmedi; (2) *bastan yanlis* — bilge-english satiri bilge-arena'nin stack'inden kopyalanmisti, o iki konteyner **hic var olmadi** (compose'da tanimli degiller, volume'lari bile yok). Yani liste tutmanin riski yalnizca "zamanla kayar" degil, **"bastan yanlis girilir ve kimse dogrulamaz"**.
> Teshis ipucu: "durmus mu, hic yok mu" ayrimini `docker ps -a` + exited/dead taramasiyla yap. Bu yapilmasaydi "servis coktu" diye yanlis teshis edilip gereksiz mudahale baslatilacakti (kesif 1487).

**KARARLAR** (olculemez — kaynagi yalniz bu dosya):
- **Klipper-first:** gozlem/otomasyon stack'i (n8n, grafana, prometheus, cadvisor, uptime-kuma) VPS'ten klipper'a tasindi.
- **VPS'te kalanlarin gerekcesi:** public domain zorunlulugu. panola.app, bilge-english, bilge-arena (data layer), plausible analytics, csp-collector (csp.3d-labx.com), social-media-server (media.3d-labx.com), dokploy stack'in kendisi ve VPS-side node-exporter bu yuzden orada.
- **Dashy bilerek VPS-only** — internal dashboard, tasima ROI'si sifir. Tekrar onerilmesin.
- **Ayni sunucuda IKI FARKLI mimari yan yana — karistirmayin:**
  - `bilge-arena` = Supabase-tarzi **katmanli**: kendi `postgrest` + `realtime` konteynerleri var.
  - `bilge-english` = Next.js → Postgres **dogrudan**: `DATABASE_URL` ile baglaniyor, **PostgREST/Realtime YOK ve olmasi da gerekmiyor** (auth ayri: GoTrue, `AUTH_URL=http://auth:9999`). Uc konteyner yeterli.
  - Bu ikisini simetrik varsaymak 2026-08'de yanlis-alarma yol acti (kesif 1487): bilge-english'te "eksik konteyner" sanildi, aslinda hic olmamislardi.

**Detay/migrasyon plani:** hafiza kaydi `architecture-vps-klipper-migration-2026-05-26`

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
