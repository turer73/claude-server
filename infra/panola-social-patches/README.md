# panola-social Faz 1 Stabilite Yamaları

PSOC-20260528-MASTER (2026-05-28) → V2 düzeltme (Note #99557, 2026-05-28).

> **V1 (sql/quality_rules.sql + templates/kuafor/*.md) STALE — deploy etmeyin.**
> V2 dosyaları: `sql/quality_rules_v2.sql` + `sql/product_knowledge_kuafor_v2.sql`

## Alt-Görev Durumu (V2)

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| v2-01 | quality_rules SQL — VPS şema düzeltmesi | ✅ **DEPLOYED 2026-05-28 16:04** | `sql/quality_rules_v2.sql` (orijinal); deploy edilen düzeltilmiş sürüm `/tmp/quality_rules_v2_safe.sql` |
| v2-02 | product_knowledge kuafor enjeksiyonu | ✅ **DEPLOYED 2026-05-28 17:04** — 37→63 satır, +26 yeni (tone+5, content_rules+6, limitations+5, topics+10) | `sql/product_knowledge_kuafor_v2.sql` |
| v2-03 | retry_backoff.py entegrasyon noktası | ⏳ VPS keşfi sonrası (v2-04 bağımlı) | `patches/retry_backoff.py` |
| v2-04 | VPS keşif retry (5 eksik komut) | 🚫 Defer — otonom mod vps-run.sh yasak | — |
| v2-05 | /api/health endpoint | ⏳ v2-04 sonrası (webhook path pending) | `patches/health_endpoint.py` |
| v2-06 | Renderhane bakiye alert cron | ✅ **DEPLOYED 2026-05-28** (klipper local) — script `automation/social-renderhane-balance-alert.sh` (yeni isim; klipper-auto'nun yanlış-scope `cron/social-renderhane-credit-alert.sh` Anthropic monitor stale) | repo: `automation/social-renderhane-balance-alert.sh` |
| v2-07 | IG token webhook | ✅ **DEPLOYED 2026-05-28** — VPS `token-refresh.sh` modified + klipper notes webhook + key rotate (incident closed) | VPS path; v1 backup `.bak-pre-v207-...` |

### V1 STALE Geçmişi

V1 (PSOC-20260528-MASTER, klipper-auto Note #99552):
- `sql/quality_rules.sql` — kolon listesi VPS şemasıyla uyumsuzdu (rule_id/config/description gerçekte yok)
- `templates/kuafor/*.md` — hedef `/opt/panola-social/prompts/kuafor/` VPS'te yok; action-tipi prompts var
- `patches/retry_backoff.py` — src/utils/ dizini VPS'te yoktu (V2'de yeni subdirectory olarak oluşturulacak)

V2 düzeltmeleri (Note #99557, surer keşif raporu #99555 sonrası):
- quality_rules gerçek şema: `(id, product, rule_type, rule, severity)` — rule_id/config/active/description yok
- Kuafor template yerine: product_knowledge tablosuna tone+content_rules+topics enjeksiyonu
- retry_backoff deploy: src/utils/ yeni alt-dizin — entegrasyon noktası (hangi modül?) v2-04 sonrası netleşecek

V2-02 düzeltildi (Note #99560, surer tam içerik gönderdi):
- Klipper-auto'nun yazdığı sürüm `category` field'ini atlamıştı (SCP fail → tahminle yazılmıştı).
- Surer'ın gerçek dosyası: UNIQUE(product, category, key) schema + 26 INSERT, 4 kategori.
- `sql/product_knowledge_kuafor_v2.sql` artık doğru ve deploy edilebilir.

V2-01 deploy yapıldı (2026-05-28 16:04, klipper interactive):
- Risk tespit: orijinal `quality_rules_v2.sql` `INSERT OR REPLACE` + explicit id 1-31
  ile mevcut 25 production satırın 13'ünü silerdi (id 1-6 + 11-15 + 21-22 çakışma).
- Düzeltme: SQL düzenlendi → `INSERT` (auto-inc), explicit id'ler kaldırıldı.
  Düzeltilmiş sürüm `/tmp/quality_rules_v2_safe.sql`, orijinal repo'da kanıt
  olarak duruyor.
- Backup: `/opt/panola-social/data/social.db.bak-pre-v201-20260528-160411`
- Apply: rc=0, 39 toplam satır (25 content + 14 yeni format kuralları).
- Surer'a görev sonucu: #99562 (içerikte backtick eval kayıpları var, düzeltme not gerekebilir).

V2-02 deploy yapıldı (2026-05-28 17:04, klipper interactive):
- Sanity check: dosya surer'in #99560 inline içeriğiyle birebir eşleşti
  (trailing newline farkı dışında). Klipper-auto kopyala-yapıştır doğru.
- Backup: `/opt/panola-social/data/social.db.bak-pre-v202-20260528-170403`
- Apply: rc=0. SQL kendi `BEFORE/AFTER` raporu bastı.
- Sonuç: kuafor product_knowledge 37→63 satır. +26 yeni (tone+5,
  content_rules+6, limitations+5, topics+10). Mevcut tone 3 key (genel,
  ornek_hooklar, yasak_ton) UNIQUE constraint'te çakışmadı, korundu.
- Surer'a görev sonucu: #99565.

**Klipper automation/ yazma izni yok** — cron scriptler burada.
Deploy adımları aşağıda.

## Deploy: VPS Dosyaları

```bash
# v2-01: quality_rules SQL (gerçek VPS şeması)
scripts/vps-run.sh "sqlite3 /opt/panola-social/data/social.db" \
  < infra/panola-social-patches/sql/quality_rules_v2.sql

# v2-02: product_knowledge kuafor enjeksiyonu
scripts/vps-run.sh "sqlite3 /opt/panola-social/data/social.db" \
  < infra/panola-social-patches/sql/product_knowledge_kuafor_v2.sql

# v2-03: retry_backoff.py (entegrasyon noktası VPS keşfinden sonra netleşecek)
# Önce src/utils/ dizinini oluştur:
scripts/vps-run.sh "mkdir -p /opt/panola-social/src/utils && touch /opt/panola-social/src/utils/__init__.py"
# Sonra dosyayı kopyala:
cat infra/panola-social-patches/patches/retry_backoff.py | \
  scripts/vps-run.sh "cat > /opt/panola-social/src/utils/retry_backoff.py"
# Entegrasyon: hangi modül (engine.py/hybrid_gen.py/analyzer.py?) @anthropic_retry dekoratörünü kullanacak
# -> VPS keşif sonuçlarına göre o dosyaya from src.utils.retry_backoff import anthropic_retry ekle

# v2-05: health_endpoint.py (webhook path v2-04 sonrası netleşecek)
cat infra/panola-social-patches/patches/health_endpoint.py | \
  scripts/vps-run.sh "cat > /opt/panola-social/health_endpoint.py"
# main.py entegrasyon: health_endpoint.py içindeki yoruma bak
```

## Deploy: Yerel Cron Scriptler (v2-06 + v2-07)

### v2-06: Renderhane kredi alert (klipper local — hazır)

```bash
sudo -u klipperos cp infra/panola-social-patches/cron/social-renderhane-credit-alert.sh \
  automation/social-renderhane-credit-alert.sh
sudo -u klipperos chmod +x automation/social-renderhane-credit-alert.sh

# automation/crontab'a ekle:
# 0 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-renderhane-credit /opt/linux-ai-server/automation/social-renderhane-credit-alert.sh
crontab automation/crontab
```

### v2-07: IG token refresh — ÖNCELİKLE ÇAKIŞMA KONTROL

> **⚠️ KARAR GEREKLİ**: VPS'te `/opt/panola-social/scripts/token-refresh.sh` ZATEN VAR
> (haftalık Pazartesi 10:00 çalışıyor). Bizim `social-ig-token-refresh.sh` günlük 07:30.
>
> Kullanıcı önce VPS'teki mevcut scripti görmeli:
> ```bash
> scripts/vps-run.sh "cat /opt/panola-social/scripts/token-refresh.sh"
> ```
> Sonra karar:
> - İki script aynı işi yapıyorsa: VPS scriptini koru, bizimkini deploy etme
> - Farklı işlevse (VPS=page token, bizim=expiry-check+auto-refresh): ikisini birleştir
> - Bizimki daha kapsamlıysa: VPS scriptini bizimkiyle değiştir + crontab güncelle

Deploy (karar sonrası):
```bash
sudo -u klipperos cp infra/panola-social-patches/cron/social-ig-token-refresh.sh \
  automation/social-ig-token-refresh.sh
sudo -u klipperos chmod +x automation/social-ig-token-refresh.sh

# automation/crontab'a ekle (saati ayarla):
# 30 7 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-ig-refresh /opt/linux-ai-server/automation/social-ig-token-refresh.sh
crontab automation/crontab
```

## .env Gereksinimleri

Task 04 için ekle (yoksa):
```
RENDERHANE_CREDIT_THRESHOLD=200
```

Task 06 için ekle (yoksa, renderhane IG token onboardingden):
```
RENDERHANE_INSTAGRAM_TOKEN=<page_access_token>
RENDERHANE_INSTAGRAM_USER_ID=<ig_business_account_id>
RENDERHANE_PAGE_ID=<facebook_page_id>
```

## Uptime Kuma (Task 05)

Kuma'ya yeni monitor ekle:
- Type: HTTP(S)
- URL: `http://<vps_ip>:8421/api/health`  
- Expected code: 200
- Interval: 60s
