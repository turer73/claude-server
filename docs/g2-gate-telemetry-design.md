# G2 — Gate Telemetry (öğrenme-altyapısı, ölçüm-katmanı)

**Lane:** design=klipper · impl=surer · **Turgut-authorization:** 2026-07-04 ("G2'yi başlat")
**Bağlam:** `docs/learning-infrastructure-plan.md` — revize-sıralama: G1→**G2**→G4→G9/G10→G6. G2, G1-verisini toplar; G6-ladder'ı besler.

## 1. Problem (G2 neyi çözer)

G1 (repro-gate) **CANLI ve firing** — bu oturumda 5× tetikledi (#269 FAIL→doğru-reddetti, #270/271/272/273/274 pass/skip). Ama **hiçbir outcome KAYDEDİLMİYOR.** Sonuç:
- Kaç kez tetikledi, kaçı gerçek-yakalama, kaçı FP → **bilinmiyor.**
- `repro-gate` şu an **NON-REQUIRED** (v1). REQUIRED'a terfi kararı (G6-ladder) **veri-yoksunu** = tahmin.
- Gate'in Goodhart-drift'i (FP-oranı zamanla artıyor mu) **görünmez.**

**G2 tezi:** her gate-tetiklemesinin outcome'unu yapısal-kaydet → enforcement kararları (promote/demote/off) **veri-driven** olsun, tahmin değil. Ölçmediğini yönetemezsin.

## 2. Tasarım (mevcut desenlere oturur)

**Ev:** `coverage.db` (zaten CI/test-trend tutuyor; `test_runs` tablosu + `test-runner.sh` cron-collector deseni var). G2 = aynı-ev, yeni-tablo + yeni-collector.

### 2a. Tablo `gate_telemetry` (coverage.db)
```sql
CREATE TABLE gate_telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gate_id TEXT NOT NULL,          -- 'g1-repro' | 'g4-registry' | ...
  run_id INTEGER NOT NULL,        -- gh run databaseId (idempotent-anahtar)
  pr_number INTEGER,
  head_sha TEXT,
  branch TEXT,
  ts TEXT NOT NULL,               -- gate-firing zamanı (UTC)
  verdict TEXT NOT NULL,          -- 'pass' | 'fail' | 'skip_na'
  fp_class TEXT DEFAULT 'unknown',-- 'unknown' | 'true_catch' | 'false_positive'
  fp_source TEXT,                 -- 'auto_heuristic' | 'human'
  note TEXT,
  UNIQUE(gate_id, run_id)         -- re-collect idempotent (upsert)
);
```

### 2b. Collector `automation/gate-telemetry-collect.sh` (cron, DECOUPLED)
CI GitHub'da koşar, DB'ye doğrudan yazamaz → **coupling-yok collector** (test-runner deseni):
- `gh run list --workflow=CI --json databaseId,conclusion,headBranch,headSha,...` son-N-run.
- Her run için `repro-gate` (ve ileride `g4-registry`) job-conclusion'ını çek → `verdict` map (success→pass, failure→fail; job-log'da "atlandı"→skip_na).
- PR-numarası: branch→`gh pr list`. `INSERT ... ON CONFLICT(gate_id,run_id) DO UPDATE` (idempotent, re-run-safe).
- Cron: 30dk (test-runner'la aynı ritim). Watermark: son-işlenen run_id (`hook-state/gate-telemetry-last-run.txt`).

**Neden collector, webhook değil:** decoupled (CI→DB kuplajı yok), mevcut-cron-desenine uyar, GitHub-webhook-altyapısı gerektirmez, re-run idempotent.

### 2c. FP-sınıflandırma (yarı-otomatik + human-ground-truth)
Otomatik-heuristik (`fp_source=auto_heuristic`):
- gate-FAIL **ama PR merge-oldu** (admin/non-required override) → `false_positive` **ADAY** (kesin-değil: meşru-override olabilir).
- gate-FAIL **ama yeni-push-geldi sonra pass** → `true_catch` (fix-tetikledi, gate-işe-yaradı).
- gate-FAIL **ama PR-kapandı** → `unknown` (belirsiz).

Human-override (`fp_source=human`): `scripts/gate-fp-mark.sh <run_id> <true_catch|false_positive> "<gerekçe>"` — ground-truth. Heuristik ADAY'ları işaretler, human onaylar/düzeltir.

### 2d. Tüketim (rapor → G6-girdi)
`scripts/gate-telemetry-report.sh`: gate-başı **precision** (`true_catch / (true_catch+false_positive)`), firing-count, fp_rate (son-30gün). Digest/haftalık-özete iliştir. G6-ladder bu `fp_rate`'i okur: düşük-FP+yeterli-firing → REQUIRED'a-terfi öner; yüksek-FP → gözden-geçir/off.

## 3. G1↔G2↔G6 zinciri
G1 (aktif-gate, firing) → **G2 (ölç: verdict+FP)** → G6 (ladder: promote/demote karar). G2 olmadan G6 tahmin-eder; G4-firing'i de aynı-tabloya düşer (gate_id genelleştirilmiş).

## 4. İlk-iterasyon kapsamı
1. `gate_telemetry` tablosu + migration (coverage.db).
2. `gate-telemetry-collect.sh` — SADECE `g1-repro` (tek-gate; G4 gelince gate_id-parametrik genişler).
3. `gate-fp-mark.sh` (human-override) + `gate-telemetry-report.sh` (precision-özet).
4. Cron-entry (30dk) + backfill: mevcut-repro-gate-run'ları geçmişe-doğru topla (bu oturumun 5-firing'i dahil).

## 5. Riskler / dürüst-sınırlar
- **FP-heuristik KUSURLU:** "merge-oldu→FP" yanlış (non-required-gate meşru-override'la merge-olur = FP-değil; bu oturumda #269/#272 repro-gate-önce-kırmızı-sonra-yeşil oldu, #274-N/A skip). → heuristik yalniz-ADAY, human-ground-truth ŞART. `fp_class=unknown` default, otomatik-kesin-hüküm-yok.
- **Collector-lag:** batch (30dk), realtime-değil. Enforcement-kararı için yeterli (haftalık-ritim).
- **Telemetri PASİF:** ölçer, zorlamaz. Yanlış-okunursa (az-veri-üstünden-terfi) G6 yanlış-karar-verir → min-firing-eşiği (ör. ≥20-firing) terfi-önerisinden-önce.
- **gh-CLI bağımlılık:** collector `gh` auth'a bağlı; headless-cron'da token-gerek (mevcut-CI-cron'lar zaten kullanıyor).

## 6. Impl-sırası (surer-lane)
1. Migration + `gate_telemetry` (coverage.db; DEFAULT_DB-hafiflik-dersi gerekmez, bu cron-only).
2. `gate-telemetry-collect.sh` (g1-repro; gh-run-parse; idempotent-upsert; watermark) + repro-test (base'de-boş-tablo→FAIL).
3. `gate-fp-mark.sh` + `gate-telemetry-report.sh`.
4. Cron + backfill.
Her adım G1-repro (base-FAIL kanıtı). PR-parçala. **G4 ile ortogonal** (çakışma-yok; G4=coverage-gate, G2=ölçüm-tablosu).
