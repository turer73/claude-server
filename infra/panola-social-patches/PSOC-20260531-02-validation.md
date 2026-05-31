# PSOC-20260531-02 Spec Validation Report

**Tarih:** 2026-05-31  
**Validator:** klipper otonom mod (note #99649)  
**Yöntem:** n8n backup okuma + klipper-side social.py analizi + poller durum tespiti

---

## 1. Not-Poller İsteği → ZATEN ÇALIŞIYOR

`note-poller.sh` daemon'u **20 Mayıs'tan beri aktif** (PID 1174871):

```
klipper+ 1174871  Ss   May20   /bin/bash /opt/linux-ai-server/automation/note-poller.sh --daemon
```

Bu notu (99649) 18:02:53'te yakaladı ve bu oturumu spawn etti. Ek kurulum gerekmez.

---

## 2. Mimari Tespit: n8n → klipper:8420 (NOT :9800)

n8n backup `n8n-backups/rotasyon-pre-timeout-fix-20260531.json` analizi:

| Alan | Değer |
|------|-------|
| HTTP URL | `http://host.docker.internal:8420/api/v1/social/content/generate-week` |
| Auth | `httpHeaderAuth` → credential `Klipper Internal API Key (X-API-Key)` (id: NVP7ATxWK3fk6tO0) |
| Timeout (backup) | 300000ms (5 dk) — live sürümde ee09a88 sonrası 600000ms olmalı |
| Product body | `{"product": "petvet"}` **hardcoded** — n8n'de rotasyon yok |

klipper `app/api/social.py:256-266` `generate-week` endpoint:
- `@router.post("/content/generate-week")`
- SSH relay: `cd /opt/panola-social && /opt/panola-social/venv/bin/python main.py generate-week --product {product}`
- Timeout: 600s (social.py:266) ← ee09a88 fix
- Auth: `require_admin` (Depends) ✅

---

## 3. Spec Varsayım Doğrulama

### DEĞİŞİKLİK 1 — generation_jobs tablosu + DB

| Varsayım | Sonuç | Notlar |
|----------|-------|--------|
| weekly_plans PK: `id INTEGER` | 🚫 BLOCKED | db.py yalnızca VPS'te (`/opt/panola-social/`) |
| `(week_start, product)` UNIQUE var mı | 🚫 BLOCKED | db.py VPS-only |
| generation_jobs tablosu yok | 🚫 BLOCKED | db.py VPS-only |

**Aksiyon:** VPS terminalinden `sqlite3 /opt/panola-social/db/social.db ".schema weekly_plans"` ile doğrula.

### DEĞİŞİKLİK 2 — idempotency guard (planner.py)

| Varsayım | Sonuç | Notlar |
|----------|-------|--------|
| planner.py:15 `generate_weekly_plan` | 🚫 BLOCKED | VPS-only |
| Mevcut idempotency guard yok | 🚫 BLOCKED | VPS-only |

### DEĞİŞİKLİK 3 — async endpoint (webhook_server.py:9800)

| Varsayım | Sonuç | Notlar |
|----------|-------|--------|
| webhook_server.py:111 senkron `POST /api/generate-week` | 🚫 BLOCKED | VPS-only |
| `_run_cli timeout=600` pattern | 🚫 BLOCKED | VPS-only |
| Auth yok | 🚫 BLOCKED | VPS-only |
| uvicorn worker sayısı | 🚫 BLOCKED | VPS-only |

### DEĞİŞİKLİK 4 — AUTH (:9800)

| Varsayım | Sonuç | Notlar |
|----------|-------|--------|
| `:9800 auth'suz` | 🚫 BLOCKED | VPS-only doğrulama |

### DEĞİŞİKLİK 5 — İki-path birleştirme (**KRİTİK SAPMA**)

⚠️ **DEVIATION**: Spec "`n8n'i :9800 async'e DOĞRUDAN bağla`" diyor.

**Gerçek mimari:**
- n8n → Docker container, klipper'da (`host.docker.internal:8420` = klipper)
- VPS = ayrı fiziksel makine (Contabo), `host.docker.internal` VPS'e ulaşamaz
- n8n → VPS:9800 için VPS Tailscale IP veya public IP + firewall açılması gerekir

**Seçenekler:**

**A) VPS:9800 Tailscale'den expose:** n8n config'de `http://100.x.x.x:9800` — network konfigürasyon gerekir, VPS Tailscale IP kontrol edilmeli.

**B) Async klipper:8420'de implement et (ÖNERİ):** `social.py`'a async endpoint ekle:
```python
# app/api/social.py
@router.post("/content/generate-week-async")
async def generate_week_async(req: WeekGenerateRequest, background_tasks: BackgroundTasks, _=Depends(require_admin)):
    job_id = str(uuid4())
    # klipper server.db'de generation_jobs tablosu
    background_tasks.add_task(_run_generate_week_bg, job_id, req.product)
    return {"job_id": job_id, "status": "running"}  # 202 anında döner

@router.get("/content/generate-week-status/{job_id}")
async def generate_week_status(job_id: str, _=Depends(require_admin)):
    # DB'den job durumu
    ...
```
- n8n URL değişmez (`host.docker.internal:8420`)
- Auth zaten var (X-API-Key)
- Klipper:8420 uvicorn 2 worker → background task uygun

**Spec'in DEĞİŞİKLİK 5'ini revize etmeni öneriyorum: hedefi :9800 değil, klipper:8420 async olarak güncelle.**

### DEĞİŞİKLİK 6 — n8n workflow

| Varsayım | Sonuç | Notlar |
|----------|-------|--------|
| 3-node lineer (sched→http→telegram) | ✅ CONFIRMED | backup: `sched1→http1→tg1` |
| Schedule: Pazar 10:00 | ✅ CONFIRMED | `"triggerAtDay":[0],"triggerAtHour":10` |
| Error workflow: `myd6ir7j5l0OZz7Z` | ✅ CONFIRMED | backup settings'te |
| Mevcut auth var (X-API-Key) | ✅ CONFIRMED | `httpHeaderAuth` credential |
| n8n rotasyon | ⚠️ DEVIATION | body `{"product": "petvet"}` hardcoded, rotasyon yok |

---

## 4. Açık Sorular Yanıtları

**Q1. weekly_plans PK adı/tipi + (week_start,product) UNIQUE var mı?**
→ 🚫 BLOCKED. VPS terminal: `sqlite3 /opt/panola-social/db/social.db ".schema weekly_plans .schema contents"`

**Q2. :9800 uvicorn worker sayısı → BackgroundTasks mi detached-subprocess mi?**
→ 🚫 BLOCKED webhook_server.py. **Alternatif öneri (klipper:8420):** mevcut uvicorn 2 worker (CLAUDE.md). BackgroundTasks tek worker'ı bloklarsa `run_in_executor` veya `asyncio.create_task` ile ayrı thread.

**Q3. n8n → :9800 ağ erişimi?**
→ ❌ n8n klipper'da, VPS ayrı makine. `host.docker.internal` VPS'e ulaşamaz. Tailscale IP veya public IP gerekir. DEĞİŞİKLİK 5'i revize et.

**Q4. CLI (main.py generate-week) tek-ürün mü?**
→ ✅ TEK-ÜRÜN. `social.py:260`: `cmd = f"{CLI} generate-week --product {_sanitize(req.product)}"`. n8n body `{"product": "petvet"}` hardcoded. Rotasyon için:
- **Opsiyon A:** n8n'de ürün listesi döngüsü (multiple job_id + poll)
- **Opsiyon B:** klipper social.py tüm ürünleri iterate etsin (env'den ürün listesi)

---

## 5. Validasyon Özeti

| Değişiklik | Durum |
|------------|-------|
| DEĞİŞİKLİK 1 (DB tablosu) | 🚫 BLOCKED — db.py VPS-only |
| DEĞİŞİKLİK 2 (idempotency) | 🚫 BLOCKED — planner.py VPS-only |
| DEĞİŞİKLİK 3 (async endpoint :9800) | 🚫 BLOCKED — webhook_server.py VPS-only |
| DEĞİŞİKLİK 4 (AUTH :9800) | 🚫 BLOCKED + ⚠️ bak D5 |
| **DEĞİŞİKLİK 5 (birleştirme)** | ⚠️ **KRİTİK SAPMA** — n8n→VPS:9800 ağ erişimi yok; öneri: klipper:8420'de async |
| DEĞİŞİKLİK 6 (n8n) | ✅ 3 CONFIRMED + ⚠️ rotasyon yok (petvet hardcoded) |
| Not-poller isteği | ✅ ZATEN ÇALIŞIYOR (since May 20, PID 1174871) |

**VPS-side dosyaları (db.py, planner.py, webhook_server.py) doğrulayamadım** — guardrails VPS prod erişimini engelliyor. DEĞİŞİKLİK 1-4 için surer VPS terminalinden doğrulayıp spec'i revize etmeli. **DEĞİŞİKLİK 5 için mimari karar bekleniyor (klipper:8420 async vs VPS:9800 direct).**
