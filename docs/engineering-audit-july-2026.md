# claude-server (linux-ai-server) Mühendislik Denetimi — 2026-07

**Denetçi:** surer (Claude, Windows)
**Tarih:** 2026-07-01
**Kapsam:** `F:\projelerim\claude-server` — tüm Python API katmanı, core modüller, automation script'leri, test altyapısı
**Yöntem:** Statik kod analizi + mimari inceleme + bağımlılık/cron haritalama

---

## 1. KRİTİK GÜVENLİK AÇIKLARI

### 1.1 ws_status.py + prometheus.py — Authentication YOK

**Dosyalar:**
- `app/api/ws_status.py` → `GET /api/v1/ws/status`
- `app/api/prometheus.py` → `GET /metrics`

**Sorun:**
Bu iki endpoint'te hiçbir authentication mekanizması yok. `main.py`'de auth middleware global değil (sadece `AuditMiddleware` ve `GlobalRateLimitMiddleware` var). Kimlik doğrulama tamamen her router'ın kendi sorumluluğunda. `ws_status.py` WebSocket bağlantı durumunu, `prometheus.py` ise CPU/memory/disk metriklerini döndürüyor.

**Etki:** Dahili sistem metrikleri + WS bağlantı durumu herkese açık. Prometheus metrikleri genelde saldırgana sistem hakkında bilgi verir (container isimleri, kaynak kullanımı, versiyonlar).

**Fix:** 10 dakika — router'a `dependencies=[Depends(require_auth)]` eklemek yeterli.

```python
# ws_status.py
from fastapi import Depends
from app.middleware.dependencies import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
```

### 1.2 dispatch.py — Import Fallback'i Auth Bypass

**Dosya:** `app/api/dispatch.py`, satır 16-21

```python
try:
    from app.api.memory import verify_key
except ImportError:
    async def verify_key() -> None:  # type: ignore[misc]
        pass
```

**Sorun:**
- `verify_key` import'u başarısız olursa (dairesel import, modül yapısı değişikliği, test konfigürasyonu) **sessizce no-op olur**
- `dispatch.py`'deki TÜM endpoint'ler (prefix `/api/v1/dispatch`) anında auth'suz kalır
- No-op olma anında **hiçbir log kaydı düşmez** — tespiti imkansız
- Bu pattern kod tabanında başka yerde yok; `dispatch.py`'ye özel bir anti-pattern

**Etki:** Kritik CLI dispatch (LLM'den gelen shell komutlarını yürütme), misconfiguration anında full shell access açığına dönüşür.

**Fix:**
```python
# Direct import, fallback yok
from app.api.memory import verify_key
```
Eğer gerçekten bir dairesel import sorunu varsa, `verify_key`'i paylaşılan bir modüle (`app/middleware/` veya `app/auth/`) taşı.

### 1.3 4 Farklı Authentication Pattern

Kod tabanında kimlik doğrulama için **4 farklı mekanizma** kullanılıyor:

| Pattern | Kullanılan Router'lar |
|---|---|
| **A: JWT Bearer + X-API-Key** (`require_auth`/`require_admin`/`require_write`) | 20 router (kernel, system, files, shell, network, ai, devops, admin, agents vb.) |
| **B: verify_key (X-Memory-Key header)** | 11 memory alt-modülü + rag.py + research.py + classifier.py + dispatch.py |
| **C: Custom per-route** | `security.py` (X-Pentest-Key veya X-Memory-Key), `telegram_bot.py` (X-Telegram-Bot-Api-Secret-Token), `csp.py` (kendi _check_key, verify_key'ı kullanmıyor) |
| **D: Auth YOK** | `ws_status.py`, `prometheus.py` |

**Sorunlar:**
- `csp.py` aynı MEMORY_API_KEY'i kullanmasına rağmen `verify_key` çağırmıyor, **kendi `_check_key`'ini yazmış** — aynı şeyin iki implementasyonu, senkronizasyon dışı kalma riski
- `deploy.py` `/memory/context` endpoint'i manuel `read_env_var("INTERNAL_API_KEY")` ile doğrulama yapıyor, JWT altyapısını bypass ediyor
- 6 router aynı statik MEMORY_API_KEY'ini kullanıyor — bu anahtar sızdırılırsa tüm sistem çöker, granüler yetki yok

**Fix:** Tüm router'ları Pattern A'ya (JWT + require_auth/require_admin) geçir. Custom pattern'leri retire et. `deploy.py`'deki INTERNAL_API_KEY kullanımını JWT ile değiştir.

### 1.4 deploy.py — Tutarsız Hata Modeli

**Dosya:** `app/api/deploy.py`

**Sorun:** Bazı hata durumlarında `raise HTTPException` kullanılırken, bazılarında 200 OK ile `{"error": ...}` dict döndürülüyor. Özellikle `get_project()` ve `deploy_project()` fonksiyonlarında "not found" durumları 200 OK ile dönüyor.

**Etki:** İstemci tarafında hata handling tutarsız — 200 OK alan client response body'sini parse edip `"error"` key'ini kontrol etmezse hatayı kaçırır.

**Fix:**
```python
# YANLIŞ:
return {"error": "Proje bulunamadı"}

# DOĞRU:
raise HTTPException(status_code=404, detail="Proje bulunamadı")
```

### 1.5 autonomous-claude.sh:488 — X-Memory-Key Boş Gönderiliyor

**Dosya:** `automation/autonomous-claude.sh`, satır 488-491

```bash
set +e
MK401=$(get_key)                # boş dönebilir, kontrol yok
curl -sf http://127.0.0.1:8420/api/v1/memory/discoveries \
    -X POST -H "X-Memory-Key: $MK401" ...   # empty header
```

**Sorun:**
- `get_key()` (satır 56-58) `.env` dosyasından `MEMORY_API_KEY` okur. Dosya yoksa veya key tanımlı değilse **boş string döndürür**
- `MK401` değişkeni hiç kontrol edilmeden curl'e gider → `X-Memory-Key: ` (empty)
- `|| true` hatayı yutar → sessiz hata, log yok
- Aynı script'in 193. satırında `handle_ack()` fonksiyonu **doğru guard** içeriyor: `[ -z "$KEY" ] && { log "MEMORY_API_KEY missing, skip"; return 1; }` — ama satır 488'de bu guard yok

**Etki:** Auth hatası sessizce yutulur. Bug discovery'leri kaybolur.

**Fix:**
```bash
MK401=$(get_key)
[ -z "$MK401" ] && { log "MEMORY_API_KEY missing (401 path), skip"; set -e; return 1; }
```

---

## 2. MİMARİ SORUNLAR

### 2.1 Router Organizasyonu — Kontrolsüz Büyüme

**Sorun:** 35+ router tek bir `app/api/` dizininde düz (flat) olarak duruyor. Her yeni endpoint eklemesiyle `main.py:24-67`'deki import listesi uzuyor. Router gruplama (ör. `app/api/memory/` gibi alt modüller) sadece memory için yapılmış.

| Kategori | Router Sayısı |
|---|---|
| ✅ İyi organize: memory | 11 (dizinde) |
| ⚠️ Flat: api/ | 24+ (düz dosyalar) |
| ❌ Karma: core/ → devops/ | 6 (devops split mixin'leri) |

**Fix:** Router'ları domain bazlı grupla:
- `api/security/` → auth.py, csp.py, security.py
- `api/deployment/` → deploy.py, vps.py, backup.py
- `api/monitoring/` → monitoring.py, prometheus.py, ws_status.py

### 2.2 10 Ölü Script

`automation/` dizininde 80+ script var. Aşağıdakiler crontab veya başka bir script tarafından çağrılmıyor (ölü kod):

| Script | Satır | Sebep |
|---|---|---|
| `autonomous-classifier.sh` | ~50 | v2 ile değiştirilmiş, eski versiyon |
| `bilge-arena-failover.sh` | 120 | Hiç deploy edilmemiş failover |
| `content-editor.sh` | 15 | Hiçbir yerde referans yok |
| `edge-log-redact.sh` | 146 | PII redactor, çalışmıyor |
| `run-agent.sh` | 53 | Manuel kullanım, otomasyonda yok |
| `social-auto-approve.sh` | 72 | crontab'da yok |
| `social-daily-publish.sh` | 44 | crontab'da yok |
| `social-token-monitor.sh` | 37 | crontab'da yok |
| `social-weekly-generate.sh` | 42 | crontab'da yok |
| `threat-detect-bwrap-poc.sh` | 83 | POC, ürünleşmemiş |

**Etki:** Kod keşfi, bakım ve onboard'u zorlaştırır. Yeni geliştirici (veya AI ajan) hangi script'in canlı hangisinin ölü olduğunu bilemez.

**Fix:** 10 script'i de archive klasörüne taşı veya sil. crontab'da referansı olmayan script = ölü.

### 2.3 Duplicate Kod (DRY İhlalleri)

**a) SSH Bağlantı Pattern'i — 3 Kopya**

| Dosya | Satır civarı | Kod |
|---|---|---|
| `app/api/social.py` | ~41 | `VPS_SSH = "ssh -o StrictHostKeyChecking=accept-new ..."` |
| `app/api/vps.py` | ~30 | `_vps_ssh()` fonksiyonu |
| `app/api/claude_code.py` | ~30 | `_run_on_vps()` fonksiyonu |

Her biri aynı SSH template'ini (`-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10`) kullanıyor. Parametre değişirse üç yerde de düzeltme gerekir.

**b) `_embed()` Fonksiyonu — 2 Kopya**

| Dosya | Satır |
|---|---|
| `app/api/rag.py` | ~85 |
| `app/api/memory/signal_quality.py` | ~93 |

İkisi de aynı Ollama embedding API'sini çağırıyor, farklı hata yönetimiyle.

**c) `TemplateResponse.json({error: ...})` — 100+ Kopya**

35 API router'ında hata yanıtları format olarak **tutarsız**:
- Bazıları: `{"error": "message", "detail": ...}`
- Bazıları: `{"error": {"type": ...}}`
- Bazıları: flat string
- deploy.py: 200 OK ile hata

**d) Health Endpoint — 4 Farklı Şema**

| Endpoint | Yanıt Şeması |
|---|---|
| `/health` (main.py) | `{status, service, version, sha, stale}` |
| `/api/v1/rag/health` | farklı şema |
| `/api/v1/research/health` | farklı şema |
| `/api/v1/memory/health` | farklı şema |

**Fix:** `_embed` → `app/core/embeddings.py`'ye taşı. SSH → `app/core/ssh_utils.py`. Health → standart şema zorunlu kıl.

### 2.4 telegram_bot.py — 6+ Sorumluluk Tek Dosyada

**Dosya:** `app/api/telegram_bot.py` (445 satır)

Bu dosya aşağıdaki sorumlulukların hepsini içeriyor:
1. Telegram webhook auth (x-telegram-bot-api-secret-token doğrulama)
2. Research routing (gelen mesajı research_agent'e yönlendirme)
3. Claude Code spawning (gelen "/claude" komutlarında çocuk process açma)
4. Inline keyboard handling (callback_query işleme)
5. Thread management (Python threading.Event cleanup)
6. Session persistence (sohbet geçmişi kaydı)
7. Agent discovery emission (bulguları memory'e yazma)

**Test coverage:** Çok düşük. Bot'taki critical code path'lerin çoğu test edilmemiş.

**Fix:**
```
app/api/telegram/
  __init__.py         → router tanımları
  webhook.py          → auth + message parsing
  research.py         → research routing
  claude_runner.py    → Claude Code spawn
  callback.py         → inline keyboard
  persistence.py      → session DB
```

### 2.5 create_discovery — 150 Satırlık Dedup Canavarı

**Dosya:** `app/api/memory/discoveries.py`

`create_discovery` fonksiyonu ~150 satır. 5 katmanlı dedup pipeline'ı:
1. **Time-window** (5dk): aynı başlık varsa skip
2. **Semantic** (embedding cosine similarity): çok benzer içerik varsa skip
3. **Exact-title**: birebir aynı başlık varsa skip
4. **Concurrent race handling**: iki eşzamanlı create varsa rollback
5. **Importance scoring**: discovery'i skorla, önemliye göre filtrele

**Sorunlar:**
- Async→sync→async thread boundary'leri
- Manual rollback + DB state management
- Cancellation pencereleri (birden fazla await noktası)
- 5 katmanlı mantık için **hiç unit test yok**

---

## 3. KOD KALİTESİ

### 3.1 research.py — 621 Satır

`app/api/research.py`, tüm API router'ları arasında en büyük dosya (621 satır). İçinde:
- 3 farklı LLM engine çağrısı (Ollama, Qwen, Claude)
- Web search integration
- Citation validation
- Sync endpoint (threadpool'da koşan blocking kod)
- Metric logging

**Fix:** `research_engine.py`, `citation.py`, `search.py` gibi yardımcı modüllere ayır.

### 3.2 14 Shell Script'inde curl timeout YOK

Aşağıdaki script'ler `curl` çağrılarında `--connect-timeout` veya `--max-time` kullanmıyor:

```
backup-monitor.sh          → api.telegram.org
backup-restore-test.sh     → api.telegram.org
daily-backup.sh            → api.telegram.org + external API
demo-reset-test.sh         → api.telegram.org
e2e-live-test.sh           → api.telegram.org
nuclei-scan.sh             → api.telegram.org + memory API
pull-vps-backup.sh         → api.telegram.org
social-auto-approve.sh     → api.telegram.org
social-daily-publish.sh    → api.telegram.org
social-token-monitor.sh    → api.telegram.org
social-weekly-generate.sh  → api.telegram.org
autonomous-spawn-retry.sh  → localhost:8420
autonomous-spawn-threat-detect.sh → localhost:8420
weekly-audit.sh            → api.telegram.org
```

**Etki:** `api.telegram.org` yanıt vermezse script sonsuza dek bekler (varsayılan TCP timeout Linux'ta 120s+). Cron job'ları birikir.

### 3.3 19 Script'te `set -e` YOK (%31)

`set -e` olmayan script'ler hata durumunda sessizce devam eder:

| Script | Risk |
|---|---|
| `daily-backup.sh` | YÜKSEK — backup scripti, curl başarısız olursa devam eder |
| `health-check.sh` | ORTA — hatalı sağlık raporu |
| `liveness-check.sh` | ORTA — false positive liveness |
| `weekly-audit.sh` | ORTA — eksik audit verisi |
| `run-agent.sh` | ORTA — sessiz hata |
| `e2e-live-test.sh` | ORTA — başarısız testi kaçırma |
| `notify-cron.sh` | DÜŞÜK — notification kaybı |
| 12 diğer script | DÜŞÜK-ORTA |

**Fix:** Her script'in başına `set -euo pipefail` ekle (eğer bilinçli bir `set +e` toggle'ı yoksa).

### 3.4 13 Script'te .env Okuma Fragil

```bash
source /opt/linux-ai-server/.env 2>/dev/null
```

`2>/dev/null` ile hata yutuluyor — dosya yoksa script sessizce çalışmaya devam eder, sonra env var'ları boş olduğu için kriptik hatalar alınır.

**İyi pattern (kullanan sadece 2 script):**
```bash
set -a
source /opt/linux-ai-server/.env
set +a
```

### 3.5 Crontab'da Tarihi Geçmiş Entry

`automation/crontab`, satır 26:
```
# 2026-05-21, self-cleaning
```
Tarih geçmiş olmasına rağmen entry hala crontab'da. "Self-cleaning" mekanizması çalışmamış.

---

## 4. TEST KAPSAMI ANALİZİ

### 4.1 Test Dosyası Sayısı: 182

Kod tabanında 182 test dosyası var — bu **nicel olarak iyi**. Ancak:

### 4.2 Noksan Testler

| Modül | Test Dosyası | Coverage | Durum |
|---|---|---|---|
| `ws_status.py` | ❌ Yok | %0 | Kritik |
| `prometheus.py` | ❌ Yok | %0 | Kritik |
| `telegram_bot.py` | `test_telegram_bot.py` var | Düşük | Bot complex code path'leri test edilmemiş |
| `dispatch.py` | `test_dispatch_api.py` var | Orta | Auth bypass senaryosu test edilmemiş |
| `mcp/tools.py` | ❌ Yok | **%45** | Hedef %80 |
| `terminal_manager.py` | ❌ Yok | **%51** | Hedef %80 |

### 4.3 Coverage Trendi

`pyproject.toml:156`: Hedef %80, eşik %75'e düşürülmüş. Gerçek coverage: ~%76.6.

### 4.4 Unit Test Eksikliği

Çoğu test **integration test** (canlı DB + HTTP client). Aşağıdaki kritik fonksiyonların **unit test**'i yok:
- `create_discovery` 5 katmanlı dedup
- `_embed` fonksiyonu
- `verify_key` edge case'leri
- SSH command chain parsing

---

## 5. ÖNCELİKLİ FİX LİSTESİ

### Hemen (1 gün)

| # | İş | Tahmin |
|---|---|---|
| 1 | `ws_status.py` + `prometheus.py` → auth ekle | 10 dk |
| 2 | `dispatch.py` → import fallback'ini kaldır, direkt import | 5 dk |
| 3 | `autonomous-claude.sh:488` → get_key() boş kontrolü | 5 dk |
| 4 | Ölü 10 script'i archive'e taşı | 15 dk |
| 5 | Crontab'daki tarihi geçmiş entry'i temizle | 2 dk |

### Kısa Vade (1 hafta)

| # | İş | Tahmin |
|---|---|---|
| 6 | SSH 3'lü duplicate → `app/core/ssh_utils.py` | 30 dk |
| 7 | `_embed` 2'li duplicate → `app/core/embeddings.py` | 20 dk |
| 8 | 14 script'e curl timeout ekle | 30 dk |
| 9 | 13 script'te .env okuma standardizasyonu | 30 dk |
| 10 | `deploy.py` hata modeli standardizasyonu | 30 dk |
| 11 | `csp.py` kendi `_check_key`'i → `verify_key`'e geçir | 10 dk |
| 12 | 19 script'e `set -euo pipefail` ekle | 20 dk |

### Orta Vade (2-3 hafta)

| # | İş | Tahmin |
|---|---|---|
| 13 | `research.py` (621 satır) → modüllere ayır | 2-3 saat |
| 14 | `telegram_bot.py` (445 satır) → `telegram/` paketi | 3-4 saat |
| 15 | `create_discovery` → unit test ekle + basitleştir | 2 saat |
| 16 | `mcp/tools.py` ve `terminal_manager.py` → test yaz | 2 saat |
| 17 | Router restructuring → domain gruplama | 1 saat |
| 18 | Health endpoint standardizasyonu | 30 dk |
| 19 | JWT auth standardization (tüm router'ları Pattern A'ya geçir) | 2 saat |

---

## 6. EK: DETAYLI METRİKLER

| Metrik | Değer |
|---|---|
| Toplam Python dosyası (app/) | ~90 |
| API router sayısı | 35+ |
| Core modül sayısı | 42 |
| Automate edilmiş script sayısı | 81 |
| Ölü script sayısı | 10 (%12) |
| Test dosyası sayısı | 182 |
| Coverage hedefi / gerçek | %80 / %76.6 |
| En kötü 2 coverage | mcp/tools.py %45, terminal_manager.py %51 |
| Auth pattern sayısı | 4 (JWT, X-Memory-Key, custom header, none) |
| curl timeout'suz script | 14 (%17) |
| set -e'siz script | 19 (%23) |
| En büyük API router | research.py (621 satır) |
| En büyük tek fonksiyon | create_discovery (~150 satır) |
