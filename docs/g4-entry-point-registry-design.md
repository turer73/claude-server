# G4 — Entry-Point Registry + ∀-Parametrize (öğrenme-altyapısı, G1'in çekirdek-çifti)

**Lane:** design=klipper · impl=surer · **Turgut-authorization:** 2026-07-04 ("G4'ü surer ile başlat")
**Bağlam:** `docs/learning-infrastructure-plan.md` PB-1 (EN-ÖNEMLİ) — *G1 GEREKLİ-AMA-YETERSİZ → G1+G4 çekirdek-ÇİFT.*

## 1. Problem (G4 neyi keser — G1'in kör-noktası)

G1 (repro-gate, CANLI) garantiler: *"repro-test bir-şey yakalıyor mu"* (base'de-FAIL = no-op-değil).
G1 **garantilemez**: *"repro-test DOĞRU yolu mu, TÜM yolları mı test ediyor."*

İki somut kanıt (bu repodan):
- **cwd/574 (yanlış-YOL):** fail-eden-teste sahipti (G1-yeşil) ama git-izolasyonunu test etti, gerçek-yazma-yolunu değil → **G1-yeşil-ama-bug-var.**
- **held-note delivery (eksik-YOL):** delivery-filter **~8 yüzeye** yayılı (`stop-check-inbox`, `session-start`, `notes.py`, `onboard.py`, `signal_quality`, `notify-cron`, `agent-feed`, **`note-poller` spawn-tetikleyici**). 4-hook-fix, `note-poller` spawn-yüzeyini **KAÇIRDI** = HOLD tamamen etkisiz (#1222 çekirdeği). Tek-yüzey test yeşil-verirken bug başka-yüzeyde yaşadı.

**G4 tezi:** cross-cutting bir invariant (delivery-filter/auth/guard) **HER giriş-noktasında** tutmalı. "tested-at-one-path" bug'ı başka-path'te saklayamamalı. Çözüm: giriş-noktalarını **registry**'den say, invariant testini **∀-parametrize** ile TÜM kayıtlı-path'lerde koş, ve registry'nin **eksiksizliğini mekanik** doğrula.

## 2. Üç bileşen

### 2a. Entry-point registry (`app/entrypoints/registry.py`)
Deklaratif, introspect-edilebilir kayıt. Her giriş-noktası:
```python
EntryPoint(id="hook:stop-check-inbox", category=HOOK,
           locator="scripts/hooks/stop-check-inbox.py",
           concerns=["note_delivery"])            # katıldığı cross-cutting invariant'lar
```
- **category:** `API | HOOK | CRON | MCP | WS`
- **concerns:** bu giriş-noktasının uyması gereken invariant-etiketleri (`note_delivery`, `auth`, `destructive_guard`, ...)

### 2b. Completeness-guard (META-gate — EN KRİTİK; registry'nin kendi-kör-noktasını kapatır)
Registry'nin *"tüm giriş-noktalarını listeliyor mu"* sorusu, çözdüğü bug-sınıfının aynısıdır. **Otomatik-keşif + registry-diff** ile mekanikleştir:
- **API:** `app.routes` introspect → her route registry'de VEYA `EXEMPT` işaretli.
- **HOOK:** `scripts/hooks/*.{py,sh}` glob → her dosya registry'de veya exempt.
- **CRON:** **canlı crontab / klipper-cron parse** (hardcode-liste DEĞİL — runtime-gerçeği) → her job registry'de.
- **MCP:** MCP tool-definitions introspect → her tool registry'de.
- **WS:** `app/ws/*.py` glob → her handler registry'de.

Yeni giriş-noktası registry'siz eklenirse → **completeness-guard CI-FAIL.** Registry sessizce bayatlayamaz (surer'ın "registry-bakımı" Goodhart-riskinin mekanik-cevabı). Keşif runtime-otoriteden beslenir (app.routes/crontab), ikinci-manuel-listeden değil.

### 2c. ∀-Parametrize invariant testleri
Her concern için, registry'den o-concern'i taşıyan giriş-noktalarını enümere eden **parametrized** test:
```python
@pytest.mark.parametrize("ep", registry.by_concern("note_delivery"), ids=lambda e: e.id)
def test_held_note_not_delivered(ep):
    assert_invariant_holds(ep)   # held(status=held) not delivered/spawned at THIS entry-point
```
Concern-etiketli ama test-path'i olmayan giriş-noktası → parametrize onu üretir → assert-yok → **FAIL** (path-coverage'ı zorlar). Bu, G1'in ikizidir: G1 "test-boş-değil", G4 "test-TÜM-gerçek-path'lerde".

## 3. G1 ↔ G4 birlikte
- Bir fix'in repro-testi G4-parametrize'den geçiyorsa: hem G1 (base'de-FAIL) hem G4 (gerçek-entry-point'ten, tüm-path'ler) sağlanır.
- **G1 tek-başına:** mock-maske/no-op keser. **+G4:** yanlış-yol + eksik-yol keser. **Çift = çekirdek.**

## 4. İlk-iterasyon kapsamı (over-mekanizasyon-freni — pilot=TEK concern)
161-API + 69-cron'un tamamı için registry kurmak Goodhart-yüzeyi patlatır. **Pilot: `note_delivery`** (somut ~8-yüzey bug-geçmişi olan, #1222-çekirdeği):
1. `note_delivery` giriş-noktaları için registry (~8 yüzey).
2. `note_delivery` completeness-guard: notes-okuyup-teslim-eden herhangi bir yüzey (grep-imza: `notes` + `read/deliver/spawn`) registry'de mi.
3. `held_not_delivered` ∀-parametrize invariant testi.
4. CI: completeness-guard + parametrize-test (başta **NON-REQUIRED**, G1-merdiveni gibi; FP-ölçülünce blocking'e terfi).

Kanıt-toplandıkça `auth` → `destructive_guard` concern'lerine genişlet (enforcement-ladder).

## 5. Riskler / dürüst-sınırlar
- **Registry-maintenance Goodhart (surer, PB):** completeness-guard otomatik-keşif ile mekanik-kapatıldı; ama keşif-imzasının kendisi eksik-olabilir (meta-meta). Azaltma: keşif runtime-otoriteden (app.routes/crontab), CRON-parse gerçek-cron'dan.
- **Invariant-ifadesi zor:** "held-not-delivered" gibi bazı invariant'lar entry-point'e-göre farklı-şekilli (API=response-filtre, hook=spawn-guard). `assert_invariant_holds` concern+category çiftine göre stratejilenmeli; tek-şablon yetmez.
- **Over-mekanizasyon GERÇEK:** pilot-tek-concern + non-required-başlangıç + evidence-ile-genişletme ile sınırla. G6-ladder'a `g4_fp_rate` telemetrisi eklenmeli.
- **G4 de yeterli-değil:** yol-coverage'ı garantiler, invariant-DOĞRULUĞUNU değil (yanlış-invariant tüm-path'lerde yanlış-geçebilir). Katman değil, çift-in-yarısı.

## 6. Impl-sırası önerisi (surer-lane)
1. `app/entrypoints/registry.py` + `EntryPoint` dataclass + `by_concern()` (saf-stdlib, hook-import-hafif — Faz-3c DEFAULT_DB dersi).
2. `note_delivery` kayıtları (~8 yüzey) + `EXEMPT`-işaretleme yardımcısı.
3. `tests/test_entrypoint_registry.py`: completeness-guard (API introspect + hook-glob + cron-parse).
4. `tests/test_invariant_note_delivery.py`: ∀-parametrize `held_not_delivered`.
5. CI job `g4-registry` (non-required v1).
6. Repro-test: her adım G1-ruhu (base'de-FAIL kanıtı).

**PR-parçalama:** 1+2 (registry iskelet) → 3 (completeness-guard) → 4+5 (invariant+CI). Ayrı-PR'lar, G1-gate'ten geçer.
