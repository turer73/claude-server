# PSOC-20260531-02 REV1 Spec Validation Report

**Tarih:** 2026-05-31  
**Validator:** klipper otonom mod (note #99651)  
**Yöntem:** Lokal kod analizi (social.py) + ISO hafta hesabı + mimari çıkarım  
**Önceki rapor:** `PSOC-20260531-02-validation.md` (note #99649 için)

---

## Bağlam: REV1 Değişiklikleri

REV1, önceki validation'daki 4 sapmayı düzeltti:
- **Sapma 1 (DEDUP)**: DEĞİŞİKLİK 0 olarak eklendi ✅
- **Sapma 2 (worker-starvation)**: BackgroundTasks yerine detached-subprocess ✅
- **Sapma 3 (auth)**: DEĞİŞİKLİK 4 olarak defense-in-depth eklendi ✅
- **Sapma 4 (hardcoded petvet)**: DEĞİŞİKLİK 6 GERÇEK ROTASYON (B) ✅
- **Kritik D5 (n8n→VPS:9800 ağ erişimi)**: **DOĞRULANDI** (surer tarafından, health 200 ✅)

---

## 2. VALIDATE AÇIK NOKTALAR — Yanıtlar

### V1: contents ↔ weekly_plans bağlantısı (dedup scope) + published-koruma

**Durum: 🚫 KISMI BLOKE** — db.py VPS-only (`/opt/panola-social/src/db.py`)

**Klipper-side çıkarım (social.py:154-176 `_SMART_APPROVE_SCRIPT`):**
```python
drafts = list_contents(status='draft', limit=50)
update_content_status(d['id'], 'approved')
update_content_status(d['id'], 'scheduled', scheduled_at=...)
```
→ `contents` tablosunun bağımsız `status` ve `id` alanları var  
→ weekly_plan_id referansı burada YOK — denormalized `(week_start, product)` composite ihtimali güçlü  
→ VEYA FK var ama smart-approve o FK'yı kullanmıyor

**DEĞİŞİKLİK 0 için kritik ön-sorgu (surer VPS'ten çalıştırmalı):**
```bash
sqlite3 /opt/panola-social/db/social.db ".schema weekly_plans; .schema contents"
```
Beklenen iki senaryo:
- **Senaryo A**: `contents(weekly_plan_id INTEGER REFERENCES weekly_plans(id))` → FK, CASCADE veya SET NULL davranışı kritik
- **Senaryo B**: `contents(week_start TEXT, product TEXT)` → JOIN by composite key, plan silinince orphan yok

**published-koruma validation:**  
Spec'in önerisi (`WHERE status != 'published'`) **mimari olarak doğru** — hangi FK yapısı olursa olsun, `status='published'` olan içerikleri DELETE dışında bırakmak güvenli. ✅

**Aksiyon:** VPS terminali `.schema` + `SELECT count(*),status FROM contents GROUP BY status;` → scenario A/B tespit + published count. DEDUP sonrası deploy.

---

### V2: main.py generate-week → job_id param + status-write eklenebilir mi?

**Durum: 🚫 KISMI BLOKE** — main.py VPS-only, ama mimari analiz yapılabilir.

**Mevcut pattern (social.py:259):**
```python
cmd = f"{CLI} generate-week --product {_sanitize(req.product)}"
```
CLI = `cd /opt/panola-social && /opt/panola-social/venv/bin/python main.py`

**Analiz:**  
Python CLI argparse extension mümkün (`--job-id UUID` eklenir). Ama:
- main.py generate-week kendi başına uzun süren bir işlem (planner + LLM + image ~5-10dk)
- main.py'ı `generation_jobs` DB'sine yazacak şekilde değiştirmek, VPS-side schema bağımlılığı yaratır
- generate-week başarıyla biterse → `done`, exception → `failed`

**Öneri: WRAPPER SCRIPT (daha temiz):**
```bash
#!/bin/bash
# /opt/panola-social/run_with_job.sh
JOB_ID=$1; PRODUCT=$2
python main.py generate-week --product "$PRODUCT"
EXIT_CODE=$?
sqlite3 /opt/panola-social/db/social.db \
  "UPDATE generation_jobs SET status='$([ $EXIT_CODE -eq 0 ] && echo done || echo failed)', finished_at=datetime('now') WHERE job_id='$JOB_ID';"
```

**Karar noktası:** Ana spec'te "yapamazsa wrapper script" — **wrapper tercih edilmeli**. main.py değişikliği defer edilebilir, tek-sorumluluk korunur. ✅

---

### V3: PRODUCTS kanonik liste + eşit/ağırlıklı rotasyon

**Durum: ⚠️ ÇIKARIM (VPS config doğrulaması bekliyor)**

**Lokal bağlam (CLAUDE.md + social.py):**
- Bilinen projeler: PetVet, Kuafor SaaS, Panola ERP → üç üründe social aktif
- social.py'da `product: str = "petvet"` default → petvet kesinlikle listede

**Önerilen kanonik liste:**
```python
PRODUCTS = ["petvet", "kuafor", "panola_erp"]
```
Bu liste **DOĞRU** görünüyor — 3 aktif proje, CLAUDE.md ile tutarlı.

**Eşit mi ağırlıklı mı?**  
Spec default eşit round-robin → **uygun**. Ağırlıklı rotasyon için VPS-side config gerekir, şu an gerek yok. ✅

**Nerede saklanmalı?**  
Spec `get_rotation_product()` in `webhook_server.py` — **PRODUCTS listesini `webhook_server.py` içinde sabit olarak tanımla**, dış config dosyasına gerek yok. Değişiklik gerekirse VPS SSH erişimi ile düzenlenir.

**VPS doğrulaması:** `grep -n "PRODUCTS\|products\|petvet\|kuafor\|panola_erp" /opt/panola-social/*.py` → mevcut config var mı kontrol.

---

### V4: get_rotation_product() — 2026-06-01 haftası hangi ürün?

**Durum: ✅ KESİN HESAPLANDI (VPS erişimi gerekmez)**

**Hesaplama:**
```python
import datetime
d = datetime.date(2026, 6, 1)
iso = d.isocalendar()
# → ISO year=2026, ISO week=23, weekday=1 (Monday)
PRODUCTS = ["petvet", "kuafor", "panola_erp"]
product = PRODUCTS[iso[1] % len(PRODUCTS)]
# → 23 % 3 = 2 → PRODUCTS[2] = "panola_erp"
```

**Sonuç: 2026-06-01 haftası → `panola_erp`**

**Smoke test güvenliği:** ❌ **GÜVENLİ DEĞİL**

- Petvet `2026-06-01` haftası için plan VAR (zaten üretilmiş) → idempotency skip olurdu
- panola_erp `2026-06-01` haftası için plan muhtemelen YOK → **GERÇEK GENERATION TETİKLENİR**
- panola_erp için LLM + image generation → Instagram API call riski
- **KULLANICI ONAYI ZORUNLU** → smoke öncesi panola_erp planı elle oluştur VEYA petvet için test hafta seçimini override et

**Güvenli smoke alternatifi:**
```bash
# Option A: manuel override ile petvet force et
curl -X POST http://VPS:9800/api/generate-week-async \
  -H "X-Webhook-Key: $KEY" \
  -d '{"product": "petvet"}'
# → petvet planı VAR → idempotency skip → güvenli

# Option B: panola_erp planı VPS'te elle oluştur önce
sqlite3 /opt/panola-social/db/social.db \
  "INSERT INTO weekly_plans(product, week_start) VALUES('panola_erp','2026-06-01') ON CONFLICT DO NOTHING;"
# → idempotency skip → güvenli
```

---

## DEĞİŞİKLİK 1 — generation_jobs DDL Syntax Doğrulama

**Durum: ✅ SQLite syntax DOĞRU**

Spec DDL:
```sql
generation_jobs(
    job_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    week_start TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    weekly_plan_id INTEGER,
    error TEXT
)
```

Doğrulamalar:
- `TEXT PRIMARY KEY` → SQLite ✅ (Postgres SERIAL/BIGSERIAL değil)
- `datetime('now')` → SQLite ✅ (Postgres `now()` DEĞİL)
- `TEXT` for dates → SQLite ✅ (ISO 8601 string olarak saklanır)
- `INTEGER` for weekly_plan_id FK → SQLite ✅

---

## DEĞİŞİKLİK 3 — async endpoint + detached-subprocess

**Klipper relay hattı hâlâ gerekli mi?**  
REV1 + D5 onayı sonrası: n8n → VPS:9800 doğrudan bağlantı çalışıyor → klipper:8420 relay artık bu akış için **gereksiz**.  
Klipper `app/api/social.py` `generate-week` endpoint'i manual/legacy kullanım için **yerinde kalabilir** (düşük öncelik).

**Detached subprocess pattern (webhook_server.py'de):**
```python
import subprocess, uuid
from pathlib import Path

@app.post("/api/generate-week-async")
async def generate_week_async(body: dict = Body({})):
    job_id = str(uuid.uuid4())
    product = body.get("product") or get_rotation_product()
    # DB'ye running kaydı
    db.execute("INSERT INTO generation_jobs(job_id,product,week_start,status,started_at) VALUES(?,?,?,?,datetime('now'))",
               [job_id, product, get_week_start(), 'running'])
    # Detached subprocess
    subprocess.Popen(
        ["/opt/panola-social/run_with_job.sh", job_id, product],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"job_id": job_id, "status": "running"}  # 202 hemen döner
```
✅ Worker starvation yok (Popen detach + start_new_session=True), ✅ job_id anında dönüyor.

---

## Validasyon Özeti

| Değişiklik | REV1 Durum | Açık Soru |
|------------|------------|-----------|
| DEĞİŞİKLİK 0 (DEDUP) | ⚠️ KISMI — published-koruma mantığı doğru; FK/composite kararı için VPS .schema gerekli | VPS `.schema contents` |
| DEĞİŞİKLİK 1 (generation_jobs DDL) | ✅ SQLite syntax DOĞRU | — |
| DEĞİŞİKLİK 2 (UNIQUE + idempotency) | ⚠️ DEDUP sonrası uygulanabilir; db.py:183 VPS-only | VPS planner.py:15 |
| DEĞİŞİKLİK 3 (async endpoint :9800) | ✅ Mimari doğru; detached-subprocess wrapper öneri eklendi | main.py veya wrapper seçimi |
| DEĞİŞİKLİK 4 (auth :9800) | ✅ X-Webhook-Key pattern uygun | — |
| DEĞİŞİKLİK 5 (iki-path) | ✅ n8n→VPS:9800 DOĞRULANDI — klipper relay bu akışta pasif | — |
| DEĞİŞİKLİK 6 (rotasyon B) | ✅ get_rotation_product() deterministik; PRODUCTS listesi doğru görünüyor | VPS config grep |
| DEĞİŞİKLİK 7 (n8n workflow) | ✅ Mimari değişiklik yok (body boş → server rotasyon) | n8n export güncellemesi |
| **V4 SMOKE GÜVENLİĞİ** | ❌ **TEHLIKELI** — 2026-06-01 ISO w23 → panola_erp (petvet değil) | **KULLANICI ONAYI ZORUNLU** |

---

## Kritik Önceliklendirme

### DEPLOY ÖNCESİ ZORUNLU (surer VPS terminalinden):
1. `sqlite3 /opt/panola-social/db/social.db ".schema weekly_plans; .schema contents"` → V1 cevabı
2. `sqlite3 ... "SELECT count(*),status FROM contents GROUP BY status;"` → published count
3. DB backup: `sqlite3 social.db ".backup social_backup_$(date +%Y%m%d).db"`

### DEPLOY SIRASI (REV1 onaylı):
1. DB backup + n8n export
2. DEĞİŞİKLİK 0 DEDUP (schema bilgisi sonrası)
3. DEĞİŞİKLİK 1+2 (generation_jobs + UNIQUE + idempotency)
4. DEĞİŞİKLİK 6 (rotasyon) + DEĞİŞİKLİK 3 (async endpoint) + DEĞİŞİKLİK 4 (auth)
5. DEĞİŞİKLİK 7 (n8n export güncellemesi)
6. **SMOKE — KULLANICI ONAYI:** 2026-06-01 haftası panola_erp → ya `{"product":"petvet"}` override ile ya da panola_erp için dummy plan oluşturulduktan sonra test et

### KODU YAZ (klipper'da implement edilebilir):
- `infra/panola-social-patches/patches/webhook_server_async.py` — async endpoint + detached subprocess pattern
- `infra/panola-social-patches/scripts/run_with_job.sh` — wrapper script (main.py değiştirmeden)
- `infra/panola-social-patches/sql/002_generation_jobs.sql` — CREATE TABLE + CREATE UNIQUE INDEX
