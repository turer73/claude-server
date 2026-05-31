# PSOC-20260531-02 REV2 Spec Validation Report

**Tarih:** 2026-05-31  
**Validator:** klipper otonom mod (note #99653)  
**Önceki raporlar:** validation.md (#99649), validation-rev1.md (#99651)

---

## REV2 Öncesi Durum

- **VQ1 (contents FK):** REV1'de BLOKE → surer #99652'de doğruladı: contents'te `weekly_plan_id`/`week_start` YOK. REV2'de acknowledged. ✅
- **VQ2 (main.py --job-id):** Surer #99652'de doğruladı: `main.py:46 cmd_generate_week` `--job-id` param kabul ediyor, wrapper gerekmez. ✅
- **4. kullanıcı kararı:** PRODUCTS = 4 (renderhane dahil). ✅

---

## 3. VALIDATE AÇIK NOKTALAR — Yanıtlar

### VQ1 (REV2): contents (product, scheduled_at) dedup kimligi güvenli mi?

**Durum: ⚠️ DEDUP ATLANDI — weekly_plans-only öneriliyor**

**Analiz (social.py:154-176 `_SMART_APPROVE_SCRIPT`):**
```python
drafts = list_contents(status='draft', limit=50)          # draft içerikler
# ...
update_content_status(d['id'], 'scheduled',
                      scheduled_at=dt.isoformat())         # scheduled_at BURADA SET EDİLİYOR
```

**Kritik tespiti:** `scheduled_at`, içerik oluşturma (generate-week) sırasında değil, **smart-approve sonrası** set ediliyor. Draft içerikler için büyük ihtimalle `scheduled_at = NULL`.

**Dedup riski:**
- Tüm draft'lar için `(product, NULL)` → hepsi aynı key → tek kayıt kalır, geri kalan silinir
- Bu, aynı ürünün farklı içerik tipleri için birden fazla draft varsa YANLIŞ sonuç üretir
- `content_type` eklense bile: draft'larda scheduled_at NULL ise hâlâ riskli

**SONUÇ: ✅ MUHAFAZAKAR KARAR — contents dedup ATLA**

- weekly_plans DEDUP (MAX(id) per week_start+product) yeterli
- UNIQUE INDEX sonrası yeni duplicate plan üretimi engellenir → future contents duplicate olanaksız
- Mevcut duplicate draft contents: düşük risk, ayrı manuel review
- `status='published'` koruma: weekly_plans-only DEDUP'ta zaten kapsam dışı ✅

**Deploy için öneri: SADECE weekly_plans DEDUP → contents dokunma.**

---

### VQ2: get_rotation_product ISO timing == planner.py week_start ISO

**Durum: ✅ TUTARLI — ama kritik implementation şartı var**

**Hesaplama (Python doğrulama):**

Her Pazar tetikleyicisi ile hedef Pazartesi DAIMA farklı ISO haftasındadır:

```
Sunday  2026-05-31 (ISO w22 → panola_erp)  ⚠️ vs  Monday 2026-06-01 (ISO w23 → renderhane)
Sunday  2026-06-07 (ISO w23 → renderhane)  ⚠️ vs  Monday 2026-06-08 (ISO w24 → petvet)
Sunday  2026-06-14 (ISO w24 → petvet)      ⚠️ vs  Monday 2026-06-15 (ISO w25 → kuafor)
Sunday  2026-06-21 (ISO w25 → kuafor)      ⚠️ vs  Monday 2026-06-22 (ISO w26 → panola_erp)
```

**SONUÇ: Bugün (Pazar) ISO'su YANLIŞ ürünü seçer. Spec'in "target week_start ISO kullan" gerekliliği KRİTİK.** ✅ Spec doğru.

**Implementation şartı:**
```python
def get_next_monday() -> str:
    today = datetime.date.today()
    days_ahead = (7 - today.weekday()) % 7  # Mon=0, Sun=6
    if days_ahead == 0:
        days_ahead = 7  # Bugün Pazartesi ise → gelecek Pazartesi
    return (today + datetime.timedelta(days=days_ahead)).isoformat()

def get_rotation_product(target_week_start: str) -> str:
    d = datetime.date.fromisoformat(target_week_start)
    iso_week = d.isocalendar()[1]
    return PRODUCTS[iso_week % len(PRODUCTS)]

# async endpoint'te:
target_monday = get_next_monday()          # Pazar → "2026-06-08"
product = body.get("product") or get_rotation_product(target_monday)
# main.py de aynı monday'i kullanmalı (ya --week-start ile pass et, ya da kendi hesabı aynı)
```

**Tutarlılık garantisi:** `main.py generate-week --week-start 2026-06-08 --job-id {id}` → planner.py week_start = 2026-06-08 (ISO w24). get_rotation_product("2026-06-08") = PRODUCTS[24%4=0] = petvet. İKİSİ AYNI ISO HAFTASINI KULLANIR. ✅

**⚠️ Dikkat:** Async endpoint `target_monday`'i hem rotasyon için hem main.py'a `--week-start` argümanı olarak geçirmeli. Planner.py'ın kendi iç hesabına bırakılırsa, Pazar gecesi → Pazartesi sınırında yarış koşulu olabilir (nadiren).

---

### VQ3: config/products.yml 4 ürün + renderhane 5-pillar

**Durum: 🚫 BLOKE — VPS-only, ama surer onayı mevcut**

VPS'te `/opt/panola-social/config/products.yml` ve `engine.py:30 load_products` erişilemez.

Surer REV2'de açıkça belirtti: "LEN=4" ve "products.yml 5 pillar config tam." Bu, surer'in VPS'ten doğruladığı bilgidir.

**Lokal doğrulama:** Mümkün değil (klipper-guardrails).

**Surer'in onayı yeterli kabul edilir** — deploy öncesi VPS'ten hızlı kontrol:
```bash
cat /opt/panola-social/config/products.yml | grep -c "product:\|name:"
grep "renderhane" /opt/panola-social/config/products.yml
```

---

## Gelecek Rotasyon Takvimi (4 ürün, ISO %4)

```
2026-06-01 (ISO w23, w23%4=3) -> renderhane  [bu hafta; smoke override ile petvet]
2026-06-08 (ISO w24, w24%4=0) -> petvet
2026-06-15 (ISO w25, w25%4=1) -> kuafor
2026-06-22 (ISO w26, w26%4=2) -> panola_erp
2026-06-29 (ISO w27, w27%4=3) -> renderhane
2026-07-06 (ISO w28, w28%4=0) -> petvet
```

**İlk doğal Pazar tetikleyicisi:** 2026-06-07 (Pazar) → hedef 2026-06-08 (ISO w24) → petvet.  
Petvet için 2026-06-08 planı muhtemelen YOK → GERÇEK üretim. Kullanıcı bilir.

---

## Smoke Test Doğrulaması

**Güvenli (surer onayı):** `POST /api/generate-week-async {"product":"petvet"}` explicit override.  
- 2026-06-01 petvet planı VAR → idempotency skip → üretim olmaz ✅
- job_id döner → poll → done → async/poll mekanizması doğrulanır ✅

---

## Validasyon Özeti REV2

| Soru | Durum | Sonuç |
|------|-------|-------|
| VQ1: contents dedup kimligi | ✅ KARAR | **ATLA** — weekly_plans-only DEDUP. scheduled_at draft'larda NULL riski. |
| VQ2: ISO timing tutarlılığı | ✅ DOĞRULANDI | target week_start ISO kullan KRİTİK (Pazar/Pazartesi her zaman farklı hafta). `--week-start` main.py'a pass et. |
| VQ3: products.yml 4 ürün | ⚠️ SURER ONAYLADI | VPS-only doğrulama, surer "LEN=4 + 5 pillar tam" dedi. |

## Tüm Değişiklikler Özeti

| Değişiklik | REV2 Durum |
|------------|------------|
| D0: DEDUP | ✅ — weekly_plans-only (contents ATLA), MAX(id) per week_start+product |
| D1: generation_jobs DDL | ✅ SQLite syntax doğru |
| D2: UNIQUE + idempotency | ✅ — DEDUP sonrası |
| D3: async endpoint + detached | ✅ — main.py:46 --job-id onaylı, Popen detach |
| D4: auth X-Webhook-Key | ✅ |
| D5: n8n→VPS:9800 | ✅ doğrulandı |
| D6: 4-ürün rotasyon | ✅ — get_rotation_product(target_week_start) KRİTİK, --week-start pass et |
| D7: n8n poll workflow | ✅ |
| Smoke | ✅ GÜVENLI — petvet explicit override |

**SAPMA: 0** (koşullu: VQ1 ATLA kararı + VQ2 --week-start zorunluluğu spec'e yansıtılsın)

**Deploy onaya hazır:** Surer VPS'ten VQ3 hızlı grep sonrası başlayabilir.
