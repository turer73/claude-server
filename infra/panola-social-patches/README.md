# panola-social Faz 1 Stabilite Yamaları

## PSOC-20260531-02 — async job+poll + gerçek rotasyon ✅ DEPLOYED 2026-05-31

panola_weekly_gen haftalık ECONNABORTED timeout + duplicate-plan idempotency bug
kapatıldı. Async `:9800` endpoint (detached) + 4-ürün deterministik rotasyon +
n8n poll workflow. Discovery-first 3 validate turu, kullanıcı onaylı deploy.
**Deployed VPS dosyalarının otoritatif kopyaları** (panola-social git repo'su yok):
`patches/PSOC-20260531-02-async-rotation/` (db/planner/main/webhook .py + MANIFEST) +
`sql/002_generation_jobs_and_dedup.sql`. Rollback + detay → MANIFEST.md.

---

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

## PSOC-20260529-02: reel_script V3 Template + Quality Rules

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| v3-template | reel_script V3 prompt template + Y2 patch (MIN 700 byte) | ✅ **DEPLOYED 2026-05-29 07:52** — VPS reel_script.md 47→65 satır; backup `.v3.bak.20260529_y2` | `templates/kuafor/reel_script_v3.txt` |
| v3-quality-rules | 5 yeni reel_script quality_rules | ✅ **DEPLOYED 2026-05-29 07:29** — 39→44 satır, id 40-44 (2 hard + 3 soft) | `sql/quality_rules_v3_reel_script.sql` |

> **Schema notu:** Not #99597'deki SQL `(content_type, rule_key, penalty, active)` şemasını kullandı; VPS gerçek şema `(product, rule_type, rule, severity)`. Adapte edildi: `content_type='reel_script'` → rule JSON'unda marker, `penalty=999` → severity='hard', diğerleri 'soft'.
>
> **Önemli bulgu (note #99615):** quality_rules yalnızca post-generation scoring'de uygulanıyor (quality_gate.py L35), generation-time inject yok. Y2 patch (`MIN 700 byte ZORUNLU` template'in HARD bölümüne) bunu telafi etti; Y1 (`engine.py`/`hybrid_gen.py` generation-time inject) Faz 3'e ertelendi.
>
> **Smoke sonucu (note #99619):** kuafor 3/3 PASS (sektor_trendi 1247B, musteri_basari 882B, salon_yonetimi 896B). Rollback: `DELETE FROM quality_rules WHERE id IN (40,41,42,43,44);`

Deploy (kullanıcı onayı gerekli — VPS):
```bash
# Template deploy (VPS dosyası: reel_script.md — Y2 patch dahil)
scripts/vps-run.sh "cp /opt/panola-social/config/templates/prompts/reel_script.md /opt/panola-social/config/templates/prompts/reel_script.v3.bak.20260529_y2"
cat infra/panola-social-patches/templates/kuafor/reel_script_v3.txt | \
  scripts/vps-run.sh "cat > /opt/panola-social/config/templates/prompts/reel_script.md"

# Quality rules SQL
scripts/vps-run.sh "sqlite3 /opt/panola-social/data/social.db" \
  < infra/panola-social-patches/sql/quality_rules_v3_reel_script.sql

# Servis reload (cache temizle)
scripts/vps-run.sh "systemctl restart panola-social"

# Y2 Smoke test: kuafor pillar='salon_yonetimi' x3, hedef 3/3 byte >= 700
# scripts/vps-run.sh "curl -s 'http://localhost:8080/api/generate' -d '{\"product\":\"kuafor\",\"pillar\":\"salon_yonetimi\",\"content_type\":\"reel_script\"}'" x3
# DB kontrol: sqlite3 /opt/panola-social/data/social.db "SELECT id, byte_sayisi FROM contents WHERE content_type='reel_script' ORDER BY id DESC LIMIT 10"
```

## PSOC-20260529-01: webhook_server.py Timeout

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| 20260529-01 | webhook_server.py subprocess timeout 300→600 | ✅ **DEPLOYED 2026-05-29 sabah** — 5 satır değiştirildi (97/122/237/256/315); backup `webhook_server.py.bak.20260529`; `panola-social-webhook.service` restart aktif | `scripts/psoc-20260529-01-webhook-timeout.sh` |

Deploy (uygulanmıştır; kanıt için):
```bash
# Doğrulama: grep timeout=300 sıfır sonuç vermeli
scripts/vps-run.sh "grep -c 'timeout=300\|timeout=600' /opt/panola-social/webhook_server.py"
```

## PSOC-20260529-04: Multi-Channel Adapter (Telegram + WhatsApp)

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| 04-adapter | `adapter/` paketi (base + telegram FULL + whatsapp SKELETON) | ✅ **Phase A DEPLOYED 2026-05-29 ~13:30** | VPS `/opt/panola-social/adapter/` |
| 04-migration | `channel_configs` + `channel_publishes` tabloları | ✅ **Phase A DEPLOYED 2026-05-29** — 3 tablo (channel_configs/channel_publishes/whatsapp_contacts) + 5 seed enabled=0 | `sql/001_channel_configs.sql` |
| 04-tests | `test_telegram.py` (smoke + mocked) | ✅ **Phase A DEPLOYED 2026-05-29** — SCP edildi | VPS `/opt/panola-social/tests/test_telegram.py` |
| 04-publisher-wire | `publisher.py` fan-out + `_record_channel_publish` helper | ✅ **Phase B DEPLOYED 2026-05-29 17:56** — 163→209 satır, dormant safe, backup `.bak-pre-psoc04b-20260529-175643` | VPS `/opt/panola-social/src/publisher.py` |
| 04-health-wire | `webhook_server.py` `/api/health` channels block additive | ✅ **Phase B DEPLOYED 2026-05-29 17:56** — 552→569 satır, mevcut keys aynen, backup `.bak-pre-psoc04b-20260529-175643` | VPS `/opt/panola-social/webhook_server.py` |
| 04-fanout-test | `test_publish_fanout.py` (fan-out skip + health channels block) | ✅ **Phase B DEPLOYED 2026-05-29 17:56** — manuel smoke 2/2 PASS (venv pytest yok) | VPS `/opt/panola-social/tests/test_publish_fanout.py` |

Phase B smoke kanıtı:
```bash
# /api/health channels block dormant:
curl -s http://localhost:9800/api/health | python3 -m json.tool
# -> "channels":{"telegram":{"status":"fail","reason":"no_token"},
#                "whatsapp":{"status":"skeleton","implemented":false,...}}

# DB state dormant:
sqlite3 /opt/panola-social/data/social.db \
  'SELECT COUNT(*) FROM channel_publishes; SELECT enabled, COUNT(*) FROM channel_configs GROUP BY enabled;'
# -> 0
# -> 0|5
```

Activation (kullanıcı eylemi, Faz 2 dışı):
1. `@BotFather` /newbot → `TELEGRAM_BOT_TOKEN`
2. `.env` ekle veya systemd `Environment=`
3. `UPDATE channel_configs SET enabled=1, config_json='{"chat_id":"@kuafor_panola"}' WHERE product='kuafor' AND channel='telegram'`
4. `systemctl restart panola-social-webhook`
5. `/api/health` → `channels.telegram.status=ok`

> **Risk notu:** Kod-side fan-out dormant. WhatsApp adapter skeleton (Meta Developer + template approval gerek).
> Deploy önkoşulu (activation): `TELEGRAM_BOT_TOKEN` env + `@BotFather` üzerinden bot + channel admin yetkisi.

## PSOC-20260529-03: Blender Render Farm Wire (Renderhane fallback)

| # | Görev | Durum | Konum |
|---|-------|-------|-------|
| 03-render-blob | Klipper SER8 `/render-blob` endpoint (binary webp stream) | ✅ **DEPLOYED 2026-05-29 16:57** — smoke 200/5314 byte/14.9s | `/opt/blender-render-farm/render_daemon.py` (klipper-local) |
| 03-hybrid-fallback | VPS `hybrid_gen.py` `_blender_fallback_bg` helper + balance/exception branch replace | ✅ **DEPLOYED 2026-05-29 17:10** — 366 satır, md5 `4725e74654377901aa78f51e41347d9e`, backup `.bak-pre-psoc03-20260529-171004` | VPS `/opt/panola-social/src/hybrid_gen.py` |

Cross-host akış (DOĞRULANDI):
```
VPS hybrid_gen._blender_fallback_bg(product_key)
  -> Tailscale http://100.84.251.49:9810/render-blob
  -> Klipper SER8 blender-render-daemon -> kuafor_salon.blend -> webp stream
  -> VPS local /opt/panola-social/assets/hybrid/blender_bg_<product>.webp
```

Helper smoke (VPS-side, helper-only):
```bash
ssh root@100.126.113.23 "cd /opt/panola-social && venv/bin/python -c '
import sys; sys.path.insert(0,\"/opt/panola-social\")
from src.hybrid_gen import _blender_fallback_bg
print(_blender_fallback_bg(\"kuafor\"))'"
# -> Blender bg hazir: 5314 byte (14713ms)
```

Rollback:
- Klipper: `/render-blob` endpoint sil + `systemctl restart blender-render-daemon` (mevcut `/render` etkilenmez)
- VPS: `cp /opt/panola-social/src/hybrid_gen.py.bak-pre-psoc03-20260529-171004 /opt/panola-social/src/hybrid_gen.py && systemctl restart panola-social-webhook`

> **4. sapma kayıt:** Surer #99633 spec'inde "Import L1-20: requests zaten var" hatalıydı — orijinal hybrid_gen.py L11-14'te yalnız `os/pathlib/datetime/PIL`. Patch öncesi discovery `import requests` eksikliğini yakaladı, eklendi.
>
> **Production tetik:** Helper smoke OK; canlı `generate_hybrid` akışı henüz Renderhane balance >= 4 path'inde (regresyon yok). Balance < 4 senaryosu doğal akışla tetiklendiğinde Blender devreye girer. Manuel izleme önerilir.

---

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
