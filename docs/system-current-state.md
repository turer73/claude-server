# Sistem Mevcut Durum — Doğrulanmış Denetim (SYSAUDIT-20260601-01)

> **Amaç:** "Yaşayan sistem" hayalinin (süreklilik / katmanlı otonomi / tam farkındalık /
> regresyon güvenliği / orkestra=Claude) önünde net, kanıta dayalı bir zemin haritası
> çıkarmak. Plan BU dokümanın üstüne kurulacak.
>
> **Kapsam:** klipper (linux-ai-server) + VPS derinlemesine. Komşu projeler
> (panola/petvet/kuafor/bilge/renderhane) yalnızca entegrasyon noktalarıyla (CI, dijest, deploy).
>
> **Kapsam-dışı (non-goals):** komşu proje iç mimarileri; yeni özellik; bu fazda HİÇBİR
> davranış değişikliği — sadece doğrulanmış gözlem.
>
> **Yöntem:** doğrulanmış audit — kod okuma + CANLI read-only kontrol (systemctl/curl/sqlite/
> docker/journal). "Kod yalan söyleyebilir" (bu oturum: VPS izleme kodu vardı, hiç çalışmıyordu).
>
> **İş bölümü:** klipper-tarafı = Claude (klipper). VPS-tarafı derin denetim = surer (içeriden).
> Senkron: notes kanalı (klipper↔surer). Sapma disiplini: §Sapma Defteri.
>
> **Durum:** ✅ A–H TAMAMLANDI (2026-06-01). klipper A–F = Claude doğrulanmış; G = surer doğrulanmış (note #99659); H = sentez. Plan bu zeminin üstüne kurulacak.

---

## A. Envanter & Sahiplik

### A.1 klipper — systemd (çalışan, doğrulandı)
- `linux-ai-server.service` — **active, enabled** (ana FastAPI app)
- `ollama.service` — active (yerel LLM)
- `docker.service` — active

### A.2 klipper — cron (klipperos, 21 iş; çoğu `klipper-cron-wrap.sh` sarmalı)
| Sıklık | İş | Amaç |
|---|---|---|
| */5 dk | alert-check.sh | alarm tarama |
| */10 dk | health-check.sh | sağlık |
| */15 dk | autonomous-spawn-retry.sh | otonom retry |
| saatlik | social-renderhane-balance-alert.sh | renderhane bakiye |
| 0 */4s | autonomous-health-check.sh | otonom sağlık |
| 03:00 | daily-backup.sh | yedek |
| 04:00 | pull-vps-backup.sh | VPS yedek çek |
| 04:00 | demo-reset-test.sh | demo reset E2E |
| 06:00 | test-runner.sh → run-all-tests.sh | **çapraz-proje test → coverage.db** |
| 06:00 | autonomous-daily-summary.sh | otonom özet |
| 07:00 | e2e-live-test.sh | Playwright E2E |
| 02:30 | memory-archive-stale.sh | hafıza arşiv |
| 03:15 | memory-triage.sh | hafıza triyaj |
| 03:30 | memory-triage-llm.py | LLM triyaj |
| Pzt 09:00 | weekly-audit.sh | haftalık denetim |
| Paz 02:00 | db-retention.sh | DB retention |
| Paz 04:00 | nuclei-scan.sh | güvenlik tarama |
| Paz 03:30 | self-pentest.sh | self-pentest |
| Paz 05:00 | claude-code-update.sh | CC güncelle |
| tek sefer | observation-week-end-reminder | hatırlatma |

### A.3 klipper — Docker (9 aktif, doğrulandı)
Gözlem: prometheus, grafana, cadvisor, node-exporter, dozzle, uptime-kuma · Otomasyon/RAG: n8n, qdrant · Araç: stirling-pdf. (Hepsi healthy; Ollama host'ta, container değil.)

### A.4 klipper — kernel modülleri (yüklü, doğrulandı)
`proc_linux_ai`, `nf_linux_ai`, `usb_linux_ai` — 3'ü de lsmod'da, refcount 0.

### A.5 klipper — FastAPI yüzeyi (doğrulandı)
- 37 route dosyası · 26 core modül · **157 endpoint** (OpenAPI)
- Arka plan: `DevOpsAgent` **çalışıyor** (124 tick, 0 aktif alarm) — 30s döngü, metrik+remediation
- (Not: bu oturumda eklenen VPS metrik toplama + CI/coverage dijest PR #9'da)

### A.6 VPS — envanter → **surer'da** (SYSAUDIT-20260601-01)
20 container (klipper probe'undan isim listesi var). Derin sahiplik/rol/Dokploy/Traefik haritası surer tarafından içeriden doğrulanacak (§G).

---

## B. State & Veri Depoları (klipper, doğrulandı)

### B.1 SQLite (aktif, /opt/linux-ai-server/data/)
| DB | Boyut | Kilit tablolar | Tazelik | Verdikt |
|---|---|---|---|---|
| server.db | 46.9MB | metrics_history(171k), audit_log(38k), vps_metrics_history(148), alerts(302), ci_lesson_learned(157), api_keys(3) | metrics/audit/vps **16:15 taze** | ✅ canlı; devops agent + VPS toplama aktif |
| claude_memory.db | 5.5MB | memories(742), sessions(1421), notes(328), discoveries(494+FTS), tasks_log(974), command_log(422), spawn_failures(2), csp_violations(1) | sessions/notes/memories **bugün taze** | ✅ canlı, en aktif depo |
| coverage.db | <0.1MB | test_runs(62) | son 06:01 bugün | ✅ günlük taze (CI kaynağı) |
| rag_metrics.db | <0.1MB | rag_queries(16) | ~2026-05, tek oturum | ⚠️ **atıl** (F-B2) |

Qdrant (6333): koleksiyonlar `klipper-memory`, `bilge-arena`.

### B.2 Bulgular
- **F-B1 — Hayalet `/var/lib/.../server.db` (0.2MB):** DB_PATH systemd'ye eklenmeden önceki eski artefakt. Operasyonel tablolar boş AMA `ci_lesson_learned` 294 satır (live'da 157 — veri ikiye bölünmüş) + artık şemada olmayan hayalet `jobs` tablosu. Kök: `ci_fixer.py:46 Database(get_settings().db_path)` env'siz çalışınca config-default `/var/lib`. digest/ci_tests ile aynı **dual-path sınıfı**. → temizlik adayı.
- **F-B2 — RAG atıl:** `rag_queries` toplam 16, hepsi tek manuel test (Windows PS, renderhane). `/api/v1/rag|research` + Qdrant `klipper-memory` kurulu ama **hiçbir otomatik/canlı akışa bağlı değil**. "Sistem kendini RAG'la bilir" → şu an aspirasyon, gerçek değil.
- **Not:** `alerts` son 2026-05-30 (2 gündür yeni alarm yok); metrics akışı taze olduğundan bu "sessiz" değil, "sakin" — yine de C/D'de teyit edilecek.
## C. Kontrol & Veri Akışı (klipper, doğrulandı)

### C.1 Tetikleme taksonomisi
| Tip | Ne | Not |
|---|---|---|
| **Polling (in-process)** | DevOpsAgent 30s döngü (metrik/detect/remediate/VPS) | tek sürekli "canlı" bileşen |
| **Cron** | 21 iş (sağlık 5-10dk, test/yedek/güvenlik/hafıza/otonom) | gerçek otonomi omurgası |
| **Olay/webhook** | alert-check + health-check (cron) + devops_agent → `/api/v1/monitor/webhooks/receive` + `/trigger/{action}` → n8n `klipper-self-healing` (webhook-tetikli) | **DAR**: sadece alert/health/remediation; tek yönlü klipper→n8n |
| **n8n-iç cron** | panola_* social, 3D LabX | n8n kendi zamanlayıcısı |
| **Pull/manuel** | **dijest** (sadece dashboard `/digest/data` açılınca + CLI) | F-C1 |

### C.2 n8n (9 aktif WF, doğrulandı)
panola social (4) + 3D LabX + Backup-Hata + Global-Error-Handler + Klipper-Self-Healing + selfHealCICD001. Çoğu içerik üretimi; sistem-farkındalık tarafı sadece self-healing + error-handler. self-healing son çalışma 2026-05-27 (olay yok = sakin).

### C.3 Bulgular
- **F-C1 — Dijest otomatik gönderilmiyor (KRİTİK):** "günlük dijest" hiçbir cron/systemd-timer'da değil; `digest.py --send` Telegram yolunu **hiçbir zamanlayıcı çağırmıyor**. Tek tetik: birisi dashboard'u açınca pull (`index.html:936`). Yani farkındalık katmanının ÇIKTISI bağlı değil — bu oturumda eklediğim VPS+CI zenginleştirmeleri dahil, kimse dashboard'a bakmazsa görünmüyor. (Hayalin "tam farkındalık" maddesinin çıktı ucu kopuk.)
- **Genel olay omurgası YOK:** aksiyonlar kendini *duyurmuyor*. Tek "olay" yolu dar (alert/health/remediation→n8n). Aggregation **PULL + periyodik**, PUSH/event değil — §"sinir sistemi yok" tezini doğrular.
## D. Farkındalık Katmanı + Sessiz-Arıza Denetimi (klipper, doğrulandı)

### D.1 Cron sağlık denetimi (per-job log, /var/log/linux-ai-server/)
**Yürütme katmanı SAĞLIKLI** (kredi hakkı): günlük işler bugün rc=0 + taze (alert/health 5-10dk, backup 03:00, test-runner 06:01, e2e 07:02, pull-vps 04:00, weekly-audit 09:00). Haftalık işler (self-pentest/nuclei/db-retention/cc-update) Pazar 2026-05-31 hepsi **rc=0** (güncel boş log = Pzt logrotate, sessiz-fail DEĞİL — F-D2 incelendi, temiz).

### D.2 Bulgular — sessiz arıza burada yaşıyor
- **F-D1 — İKİ ayrı E2E suite, ikisi de rc=0 ile maskeli (canlı, doğrulandı):**
  - **demo-reset-test (cron 04:00, Panola):** **8/123 pass — 115 FAIL.** Kök: demo seed 0 kayıt üretiyor (`Müşteri:0/Ürün:0/Sipariş:0`) ama "✅ tamamlandı" diyor → testler login/veri bulamayıp timeout. = açık **bug #493** (gotrue signUp). Günlük 115 fail; Telegram'da doğru yazıyor ama aksiyon yok.
  - **e2e-live-test (cron 07:00, çok-proje):** **31/3** (rekürsif=shallow sayım uyuştu, parser sağlam — önceki "shallow bug" hipotezi GERİ ALINDI). 3 fail: panola login + panola dashboard (ikisi de waitForURL timeout = #493 ile aynı kök), kuafor sidebar nav 0 (ayrı).
  - **Maske mekanizması:** her iki cron wrapper `rc=0` döndürüyor (playwright fail → script içi yutuluyor); dijest çekmiyor (F-C1); alarm yok. **rc=0 = "çöktü mü", "amacını yaptı mı" DEĞİL.** Panola E2E ~117 testtir kırık, sistem "yeşil-ish" sanıyor.
- **Çekirdek tespit:** işler iyi *çalışıyor* (refleks), ama sistem kısmi-başarısızlığı *fark etmiyor* (farkındalık yok). Düşman tam "job rc=0" ile "job amacını yaptı" arasındaki boşlukta. Hafızadaki ~tüm sessiz-arıza vakaları (test-runner 9 gün, health-check 36h, nuclei sahte-0, demo-reset) aynı kalıp.
- F-C1 ile birleşince: yerel log var, ama **toplayan/fark eden kimse yok** → arıza ancak biri elle bakınca (ya da bu denetim gibi) görünür.
## E. Otonomi Katmanı (klipper, doğrulandı)
- **Mod:** supervised. **Hat:** not → classifier (qwen2.5:7b, 4-sınıf ACK/ACTIONABLE/DISCUSSION/URGENT) → autonomous-claude spawn. Cron: spawn-retry 15dk, health 4s (ikisi rc=0).
- **F-E1 — Sınıflandırma çıktısı 2026-05-28'den beri durmuş:** memories ack=16/spawn=18/deferred=11 hepsinin son tarihi **05-28**; 4 gündür yeni otonom sınıf yok. Cron'lar dönüyor ama üretim sıfır → ya kuru girdi ya sessiz stall (teyit gerek: autonomous-claude ne tüketiyor).
- **F-E2 — Oto-müdahale (action kolu) atıl:** DevOpsAgent 124+ tick, 0 aktif alarm, **toplam 0 remediation**. Playbook'lar (restart/prune) mevcut run'da hiç çalışmamış → düşük güven (hafıza: baseline FP 32k alarm geçmişi). Otonominin "algı" var, "eylem+doğrulama" kolu kanıtlanmamış.
- Drift geçmişi (hafıza): PSOC stale, halüsinasyon, spec sapmaları → otonomi doğrulamasız sapıyor.

## F. Regresyon Güvenliği / Blast-Radius (klipper, doğrulandı)
- **En güçlü varlık:** coverage.db çapraz-proje test ağı — son run **3634/3634 pass** (10 proje, günlük). Gerçek regresyon kalkanı bu.
- ci.py test-çalıştırma API + ci_fixer auto-fix (ci_lesson_learned 157 ders). E2E ayrı ağ (ama F-D1: 3 fail maskeli).
- **F-F1 — Blast-radius/etki analizi YOK:** bağımlılık/etki haritası aracı yok (grep temiz). "Bir iş diğerini bozmasın" garantisi = değişiklik SONRASI testin yakalaması; değişiklik ÖNCESİ neye dokunduğunu bilme yok → elle yapılıyor (bu oturum: ci_db retire öncesi elle grep). Hayalin 4. maddesinin proaktif yarısı eksik.
## G. VPS Entegrasyonu (surer-lead, doğrulandı — read-only SSH, note #99659)
Host vmi3160046 Ubuntu24.04.4 kernel6.8.0-110, uptime 36g. **20/20 container running, 0 restart** (crash-loop yok).

- **G.1 Envanter:** Edge: dokploy-traefik v3.1 (:80/:443 public, distroless healthcheck gizli — #356 doğru) + dokploy v0.29.2. Public-prod: bilge-arena (postgrest/realtime, "-dev" subdomain), bilge-english (app/auth/pg), panola (KENDİ panola-caddy:8080 edge'i + auth/pg/postgrest, hepsi 127.0.0.1), plausible (app/pg/clickhouse). Internal: csp-collector, dashy, social-media-server, node-exporter (sadece-Tailscale).
- **G.2 Sağlık:** 13 healthcheck PASS, 7 no-healthcheck ama HTTP-probe canlı. VPS cron 11 iş (backup/bilge-arena sync/rooms-relay her-dakika/edge-redact KVKK/token-refresh). **Sessiz-fail tespit edilmedi** (token-refresh bugün 10:00 çalıştı; CrowdSec IPS kurulu).
- **G.3 Kaynak:** 6 core ~%30 yük, RAM 4.2/11Gi (rahat), disk 37G/193G (%20). ClickHouse top-CPU (background merge). Çoğu container resource-limit'siz.
- **G.4 SSH köprüsü:** Tailscale klipper-2→VPS root **canlı, identity-based**. sshd sağlıklı (root prohibit-password, pubkey-only).
- **G.5 Migration:** **DOĞRULANDI** — 5 servis (n8n/grafana/prometheus/cadvisor/uptime-kuma) VPS'te YOK, klipper'da healthy. (ama orphan volume'leri VPS'te duruyor + yedekleniyor.)
- **G.6 Backup:** **ÇİFT katman, ikisi de bugün başarılı** — push (R2 7g + GDrive 30g, 06-01 03:15 1.9G) + pull-klipper (06-01 04:00 6vol 46M).

### G.7 surer'ın 9 sapması (→ Açık Liste)
| # | Sapma | Tip |
|---|---|---|
| G-1 | **Port 22 0.0.0.0'da açık** + public-giriş var (hafıza "kapalı" = STALE/yanlış) | 🔴 güvenlik |
| G-2 | root authorized_keys'te **2 stale Coolify key** (04-07 kaldırıldı, key duruyor) | 🔴 güvenlik |
| G-5 | **Backup Uptime-Kuma push'u ölü klipper IP'sine** (100.113.153.62, 05-13 decommissioned) → `\|\| true` yutuyor = sessiz monitoring-fail | 🟡 sessiz arıza |
| G-6 | VPS'te orphan volume (grafana/prometheus/n8n) + **ölü veri yedekleniyor** | 🟢 temizlik |
| G-3,4,8 | **CLAUDE.md stale:** klipper "2 container"→9; VPS hâlâ taşınan servisleri+OpenClaw listeliyor (OpenClaw hiç yok); "17 VPS-only"→20 | 🟢 doküman |
| G-7 | VPS kernel drift 6.8.0-107→110 | 🟢 |
| G-9 | bilge-arena api/ws "-dev" subdomain (prod/dev belirsiz) | 🟡 doğrulanmalı |

**Pozitif:** migration gerçek+sağlıklı, backup 2-katman bugün OK, 0-restart, CrowdSec+KVKK-redact aktif, köprü canlı, kaynak rahat.

---

## H. SENTEZ — 5-Yetenek Olgunluk + Açık Liste

### H.1 Olgunluk (hayale karşı, doğrulanmış)
| Yetenek | Durum (kanıt) | Olgunluk |
|---|---|---|
| 1. Süreklilik | cron+agent+n8n sürekli dönüyor, 0-restart; ama "canlı beyin" yok | 🟡 orta |
| 2. Katmanlı otonomi | hat var ama sınıflandırma 05-28'den atıl (F-E1), remediation 0/dormant (F-E2), doğrulamasız drift | 🟠 düşük-orta |
| 3. **Tam farkındalık** | dijest push edilmiyor (F-C1), olay omurgası yok, çok sayıda sessiz arıza (F-D1 Panola ~117 fail, G-5, F-B2 RAG) | 🔴 **düşük (en zayıf)** |
| 4. Regresyon güvenliği | güçlü test ağı (coverage 3634) AMA blast-radius yok (F-F1) + ağın failleri fark edilmiyor (F-D1) | 🟡 orta (reaktif) |
| 5. Orkestra=Claude | oturumluk, stateless, kalıcı beyin değil | 🟠 düşük |

### H.2 Çekirdek tespit
**Tek baskın kalıp: doğrulanmış öz-farkındalık yok → sessiz arıza birikiyor.** Düşman "çalıştı (rc=0)" ile "amacını yaptı" arasındaki boşluk. Tespit edilen sessiz/maskeli arızalar: F-C1 (dijest bağlı değil), F-D1 (Panola E2E ~117 fail/#493, günlük, maskeli), F-E1/E2 (otonomi atıl), F-B2 (RAG atıl), G-5 (backup push ölü host). Hepsi aynı kök.

### H.3 Tam Açık Liste (plan girdisi)
**🔴 Güvenlik:** G-1 (port22), G-2 (stale key) · **🔴 Farkındalık:** F-C1 (dijest push), F-D1 (E2E maske + #493) · **🟡 Sessiz arıza:** G-5 (backup push), F-E1/E2 (otonomi) · **🟢 Temizlik/doküman:** F-B1, F-B2, G-6, G-3/4/8 (CLAUDE.md), G-7, F-F1 (blast-radius — yapısal).

### H.4 Plan için yön (öneri, henüz plan değil)
En yüksek kaldıraç = **Yetenek 3 (farkındalık) + sessiz-arıza eliminasyonu**: (a) her veri kaynağı/iş "canlı+taze+amacını-yaptı mı" meta-kontrolü, (b) dijest push'u + olay omurgası, (c) "rc=0 ≠ başarı" — iş çıktısını doğrulayan sarmalar. Güvenlik (G-1/G-2) ayrı/acil. Plan bunları fazlara böler; her faz Sapma Defteri + gate ile.

---

## Sapma Defteri (Drift Register) — denetim bulguları yukarıda (F-*/G-*); bu tablo PLAN YÜRÜTME sapmaları için
| Tarih | Bölüm | Sapma | Karar |
|---|---|---|---|
| 2026-06-01 | F-D1 | Claude önce demo-reset(Panola) artefaktını e2e-live ile karıştırdı, "shallow parser bug" dedi | Geri alındı, iki suite ayrıştırıldı, doğru sayım |

---

## Sapma Defteri (Drift Register)
Plan/varsayım gerçekle çeliştiğinde: DUR → buraya yaz (ne + neden) → karar (rotayı düzelt / planı onayla-güncelle). Sessiz kayma yasak.

| Tarih | Bölüm | Sapma | Karar |
|---|---|---|---|
| — | — | (henüz yok) | — |
