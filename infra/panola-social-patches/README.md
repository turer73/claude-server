# panola-social Faz 1 Stabilite Yamaları

PSOC-20260528-MASTER (2026-05-28). Surer kaynak dosyaları SCP ile alınamadı
(klipper-auto izin kısıtı), tüm dosyalar görev açıklamalarından sıfırdan yazıldı.

> **⚠️ STALE — DEPLOY ETMEYIN (2026-05-28 klipper interactive review).**
>
> Klipper interactive bu paketi VPS gerçeğiyle karşılaştırdı; **klipper-auto
> hiç VPS keşfi yapmadığı için 3 mismatch tespit edildi:**
>
> 1. **`sql/quality_rules.sql`** — INSERT kolon listesi (`rule_id`, `config`,
>    `description`) VPS gerçek `quality_rules` tablosunda **yok** (gerçek:
>    `id, product, rule_type, rule, severity, created_at`). SQL hata atar.
> 2. **`patches/retry_backoff.py`** — README "Deploy:" satırı
>    `/opt/panola-social/src/utils/retry_backoff.py` der; **`src/utils/`
>    dizini VPS'te yok** (`src/` flat). Hangi modül Anthropic çağırıyor
>    (engine.py / hybrid_gen.py / analyzer.py?) belirlenmedi.
> 3. **`templates/kuafor/*.md`** — README hedefi
>    `/opt/panola-social/prompts/`; bu dizin VPS'te **yok**. Gerçek
>    prompt'lar `config/templates/prompts/` altında **action-tipi**
>    isimlerle (`before_after.md`, `educational_carousel.md`, ...). Kuafor
>    için ayrı dizin/konvansiyon belirsiz.
>
> Bu paket commit'lendi sadece **version-controlled reference** olarak.
> Düzeltilmiş patches surer'dan gelen yapısal bilgiyle yeniden yazılacak
> (görev sonucu notu: #99553). Bu paketin "Deploy" adımları aşağıdaki
> dosyalarda **olduğu gibi koşulmamalı.**

## Alt-Görev Durumu

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| 02 | quality_rules SQL | ⚠️ STALE | `sql/quality_rules.sql` |
| 01 | Kuafor template tone fix (6 adet) | ⚠️ STALE | `templates/kuafor/*.md` |
| 03 | Anthropic retry_backoff.py | ⚠️ STALE | `patches/retry_backoff.py` |
| 05 | /api/health endpoint | ✅ Hazır (path/integration bilgisi pending) | `patches/health_endpoint.py` |
| 04 | Renderhane kredi alert cron | ✅ Hazır (klipper local, hedef path validation pending) | `cron/social-renderhane-credit-alert.sh` |
| 06 | IG token auto-refresh cron | ✅ Hazır (klipper local, IG token storage path pending) | `cron/social-ig-token-refresh.sh` |

**Klipper automation/ yazma izni yok** — cron scriptler burada.
Deploy adımları aşağıda.

## Deploy: VPS Dosyaları (04 + 05 hariç)

```bash
# Task 02: quality_rules SQL
# VPS panola-social DB'ye uygula:
scripts/vps-run.sh "sqlite3 /opt/panola-social/data/social.db" \
  < infra/panola-social-patches/sql/quality_rules.sql

# Task 03: retry_backoff.py
# VPS'e kopyala:
cat infra/panola-social-patches/patches/retry_backoff.py | \
  scripts/vps-run.sh "cat > /opt/panola-social/src/utils/retry_backoff.py"

# Task 05: health_endpoint.py
# VPS'e kopyala:
cat infra/panola-social-patches/patches/health_endpoint.py | \
  scripts/vps-run.sh "cat > /opt/panola-social/health_endpoint.py"
# main.py'ye entegrasyon (health_endpoint.py içindeki yorumu gör)

# Task 01: kuafor templates
# VPS prompts dizinine kopyala:
for f in infra/panola-social-patches/templates/kuafor/*.md; do
  fname=$(basename "$f")
  cat "$f" | scripts/vps-run.sh "cat > /opt/panola-social/prompts/kuafor/$fname"
done
```

## Deploy: Yerel Cron Scriptler (04 + 06)

```bash
# Automation dizinine kopyala (klipperos kullanıcısıyla)
sudo -u klipperos cp infra/panola-social-patches/cron/social-renderhane-credit-alert.sh \
  automation/social-renderhane-credit-alert.sh
sudo -u klipperos chmod +x automation/social-renderhane-credit-alert.sh

sudo -u klipperos cp infra/panola-social-patches/cron/social-ig-token-refresh.sh \
  automation/social-ig-token-refresh.sh
sudo -u klipperos chmod +x automation/social-ig-token-refresh.sh

# automation/crontab'a ekle:
# 0 * * * *  /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-renderhane-credit /opt/linux-ai-server/automation/social-renderhane-credit-alert.sh
# 30 7 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-ig-refresh /opt/linux-ai-server/automation/social-ig-token-refresh.sh

# Crontab'ı yükle:
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
