# FAZ 4 — Blast-radius / değişiklik-öncesi etki (scoping, klipper)

**Durum:** SCOPING (build YOK). FAZ 3 kapanmadan (notify-cron surer-gated) FAZ4'e zemin. Bu doküman ilk buildable slice'ı belirler; build kullanıcı go'su sonrası.

## Amaç
Bir değişiklik **neye dokunduğunu ÖNCEDEN** bilsin (şu an elle grep). Hafif statik etki-haritası: değişen dosya → (a) dokunduğu DB tabloları (b) onu import eden / ona bağlı modüller (reverse-dep). PR-review sistemine (FAZ1 canlı/#15) beslenir: review-prompt "bu PR `events` tablosuna dokunuyor; consumer'lar: digest, devops_agent, notify-cron" diyebilir.

## Gerçek mimari (zemin, 2026-06-03 ölçüldü)
- 37 route dosyası (`app/api/`); **sadece 8'i** direkt SQL/`db.execute`. Çoğu **route → core/ servisi → DB** (örn. `digest.py` route → `core/digest.py` → tablolar).
- Yani naif "route'ta SQL grep" yetmez; **2-hop** izlemek gerekir: route → core-modül → tablo.
- Mevcut bağımlılık aracı YOK → sıfırdan ince slice.

## İlke (mevcut araç-felsefesiyle tutarlı)
Heavyweight AST/import-graph kütüphanesi DEĞİL (over-engineer). Önce **grep-tabanlı hafif mapper** (cron_outcomes/emit-event.sh deseni gibi deterministik + basit). Yeterli olmazsa AST'ye yüksel.

## İlk buildable slice (öneri — S1)
`scripts/blast-radius.sh <degisen-dosya>`:
1. **Forward (dokunduğu tablolar):** dosyada + import ettiği `app.core.*` modüllerinde `INSERT INTO|FROM|UPDATE|CREATE TABLE` grep → tablo listesi.
2. **Reverse (kim bağlı):** `grep -rl "import <modul>"` + tabloyu okuyan diğer modüller (`grep -rl "FROM <tablo>"`) → etkilenen consumer'lar.
3. Çıktı: kısa rapor (`dosya → tablolar: [...] | consumers: [...]`). PR-review-poller'ın review-prompt'una opsiyonel ek.
- Test: `events.py` → tablo `events`; consumers `digest/devops_agent/cron-wrap/pull-vps-backup` (FAZ3.2 wiring) doğru çıkmalı (kendi-üstümüzde dogfood).

## Slice durumu
- ✅ **S1** (#22): tek-dosya forward(2-hop)+reverse. (route→core hop S1'in 2-hop'unda zaten var.)
- ✅ **S2** (bu PR): `--diff [range]` changeset-mode — değişen TÜM dosyaların AGREGAT etki-haritası
  (varsayılan range `@{u}...HEAD`, fallback `HEAD~1`; değişen-set consumer'lardan hariç).
  + FP-filtre iyileştirme: `FROM` yalnız SELECT-içeren satırda sayılır → docstring-prose
  ("from tracking") elenir, gerçek `SELECT..FROM t` kalır. Dogfood: #28 changeset temiz.
- ⬜ **S3:** PR-review entegrasyonu (poller `blast-radius --diff`'i review-prompt'a enjekte).
- ⬜ **S4:** "yüksek-blast" PR'larda ekstra-dikkat sinyali (FAZ2 review-spawn kararına girdi).

**Bilinen limit (grep):** çok-dosya changeset'lerde nadir prose-FP kalabilir; kesin
ayrım AST gerektirir (gerekirse S5). Tek-DB kaynak modülleri + SELECT-FROM net.

## Gate
S1 build = kullanıcı go. FAZ4'ün geri kalanı FAZ3 (notify-cron) kapandıktan sonra önceliklenir.
