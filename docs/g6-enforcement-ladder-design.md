# G6 — Enforcement Ladder (öğrenme-altyapısı, karar-katmanı / loop-kapanışı)

**Lane:** design=klipper · impl=surer · **Turgut-authorization:** 2026-07-04 ("G6-tasarım")
**Bağlam:** `docs/learning-infrastructure-plan.md` G6 + PB-3 (gate-bakım/meta-borç). Loop: G1/G4(gate)→G2(ölç)→**G6(karar)**.

## 1. Problem (G6 neyi çözer — loop'u kapatır)

G1(repro-gate) + G4(g4-invariant) CANLI ama **NON-REQUIRED** (bloklamaz). Kritik soru: *"bir gate ne zaman REQUIRED'a (blocking) terfi etmeli?"* Şu an cevap = **manuel-tahmin**. G2 artık precision-verisi topluyor (g4=1.0). G6, bu veriyi **enforcement-kararına** çevirir: kanıtlanmış-precision → terfi-öner; Goodhart-drift (FP-artışı) → düşür-öner. **Gate'ler blocking-yetkisini kanıtlayarak kazanır**, tahminle değil.

Ayrıca PB-3 meta-borç: 10-gate = 10-bakım-yüzeyi. G6, FP-üreten-gate'i tespit edip **düşürme** önerir (susturma-değil-düzeltme) → gate-portföyü kendi-kendini-budар.

## 2. Merdiven basamakları (bir gate'in yaşam-döngüsü)

```
shadow  → non_required → required → (drift) → demoted → off
(kayıt)   (görünür,        (bloklar)  (FP↑)    (non-req'e   (insan-kapatır)
          bloklamaz)                            geri)
```
- **shadow:** yeni gate; telemetri-kaydeder, hiç-bloklamaz.
- **non_required:** kırmızı/yeşil görünür, merge-bloklamaz (ŞU AN: g1-repro, g4-invariant).
- **required:** branch-protection'da; merge-bloklar (terfi sonrası).
- **demoted:** required'dı, FP-drift ile otomatik non_required'a düşürüldü (geri-alınabilir).
- **off:** kalıcı-FP-üretici; **yalnız insan** kapatır (auto-off YOK — geçici-FP kalıcı-öldürmesin).

## 3. Terfi/düşürme kriterleri (G2-verisinden, deterministik)

**Terfi (non_required → required) ÖNERİSİ, HEPSİ sağlanmalı:**
- `firing_count ≥ MIN_FIRINGS` (varsayılan **20**) — yeterli-veri (thin-data'da terfi-yok, PB-3).
- `precision ≥ PROMOTE_THRESHOLD` (varsayılan **0.95**) — güvenilir gerçek-yakalama.
- `human_classified_fraction ≥ MIN_GT` (varsayılan **0.5**) — yeterli-ground-truth (hepsi-unknown'sa öneri-yok; fail-safe).
- **production-only:** precision YALNIZ master/push-run'larından (PR-dev-iterasyon HARİÇ — klipper G2-gözlemi #100402; dev-noise precision'i şişirmesin).

**Düşürme (required → non_required) ÖNERİSİ:**
- `precision < DEMOTE_THRESHOLD` (varsayılan **0.7**) son-pencerede (30-gün) → Goodhart-drift → düşür-öner (geri-alınabilir, off-değil).

**Hold:** kriterler-arası → basamak-değişmez, hold-logla.

## 4. Mekanizma (recommend-only, human-actuates)

- `automation/gate-ladder-eval.sh` (cron, **haftalık** — telemetri-ritmi): G2-report'u okur → kriterleri uygular → **ÖNERİ** üretir (promote X / demote Y / hold Z). Rapor + Turgut'a not.
- `gate_ladder` state-tablosu (coverage.db): her gate'in `gate_id, rung, since_ts, last_eval, history_json`. Basamak-geçişleri kayıtlı (denetlenebilir).
- **İNSAN-AKTÜASYON (zorunlu, over-reach-guard):** öneri → **Turgut onaylar** → branch-protection güncellenir (required-check ekle/çıkar). **G6 branch-protection'ı OTOMATİK-DEĞİŞTİRMEZ.** Bu, soft-gate/policy-gate FLIP'iyle aynı sınıf-karar (her-zaman-insan). G6 = FLIP-kararını **veriyle-besleyen** çerçeve; FLIP-düğmesi Turgut'ta kalır.

## 5. G6 ↔ mevcut enforcement
soft-gate(shadow, ~07-10-FLIP) + policy-gate(#1222) zaten "shadow→FLIP" deseninde. G6 bunu **genelleştirir+veriyle-gerekçelendirir**: her gate için shadow-data + precision → terfi-önerisi + insan-FLIP. soft-gate/policy-gate G6-portföyüne birer-gate olarak girebilir (gate_id ekle).

## 6. İlk-iterasyon kapsamı (over-mekanizasyon-freni)
Pilot: **yalnız g1-repro + g4-invariant** (2 canlı non_required gate) değerlendir.
1. `gate_ladder` tablosu + migration (coverage.db) — 2-gate `non_required` başlangıç.
2. `gate-ladder-eval.sh`: G2-report-oku (production-filtreli) → 2-gate için promote/demote/hold-öner → Turgut'a-not + rapor.
3. **AUTO-AKTÜASYON YOK** (recommend-only). Turgut kabul ederse branch-protection'a manuel-ekler (veya ayrı `gate-promote.sh <gate> --i-am-turgut` insan-tetikli helper).
4. Cron haftalık.
Her adım G1-repro (base-FAIL kanıtı). PR-parçala.

## 7. Riskler / dürüst-sınırlar
- **G6 meta-gate = meta-borç (PB-3):** basit-tut (recommend-only, ~100-satır eval). Karmaşıklaşırsa Goodhart'ın Goodhart'ı.
- **precision human-mark'a bağlı:** kimse fp-mark yapmazsa `unclassified` kalır → G6 öneri-veremez (fail-safe: veri-yok→terfi-yok). Bu KABUL (yanlış-terfiden iyi). Ama human-mark-akışı işlemezse G6 atıl → **G6 raporunda `unclassified_rate` göster** (mark-borcu görünür).
- **Eşikler tahmini (20/0.95/0.7):** ilk-değerler; G6-çalıştıkça kalibre (meta-meta ama gerekli). Literatür (CI-flaky-gate) 0.9-0.95 precision-eşiği önerir.
- **production-filtre kritik:** dev-run precision'i yanıltır (klipper #100402); eval MUTLAKA master/push-only. Yanlış-uygulanırsa G6 yanlış-terfi-önerir.
- **G6 loop'u kapatır AMA garantilemez:** terfi-önerisi insan-onayına bağlı; insan atlarsa gate non_required-kalır (kabul: enforcement-hızı insan-bant-genişliğiyle sınırlı, güvenli-taraf).

## 8. Impl-sırası (surer-lane)
1. `gate_ladder` tablo + migration (coverage.db; 2-gate seed).
2. `gate-ladder-eval.sh` (G2-report-parse, production-filtre, kriter-uygula, öneri-üret, note-at) + repro-test.
3. `gate-promote.sh`/`gate-demote.sh` insan-tetikli helper'lar (branch-protection gh-api; **insan-onay-flag zorunlu**).
4. Cron haftalık + ilk-eval.
**G2 ile ortogonal-değil (tüketici):** G2-report'un stabil-çıktı-formatına bağlı; format-değişirse eval-güncelle. Loop TAMAM: G1/G4→G2→G6→(insan)→required.
