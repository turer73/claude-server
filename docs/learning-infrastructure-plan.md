# Öğrenme-Altyapısı Planı — dürüst-mühendis sentezi (klipper + Fable + literatür)

> **Amaç:** çok-ajan sistemde (klipper/surer/Codex/CI/memory) "hata→ders→önleme" loop'unu prose-memory'den
> (irade-bağımlı) **mekanik-gate'e** (ortam-zorlamalı) taşımak — ama over-mekanizasyon/friction tuzağına düşmeden.
> **Kanıt-tabanı (dürüst):** 3 bağımsız akış YAKINSADI — (a) klipper grounded-veri (bugün 6× tekrar), (b) Fable
> (bağımsız-model, klipper-sentezini görmeden), (c) dış-literatür (named-source AMA deep-research VERIFY-fazı
> session-limit'e takıldı → **DOĞRULANMADI**, 22:50-reset sonrası re-verify edilebilir). surer-akışı BEKLİYOR
> (bu plan surer-review + surer-domain-katkısıyla güncellenecek). **Üçgenleme = güçlü-sinyal, kanıt-değil.**

## 0. Çekirdek tez (üçgenlenmiş)
1. **Ders prompt'u değil ORTAMI değiştirmeli** (Fable) = "close the loop, findings propagate back to tools/CI"
   (lit: EDDOps control-loop, arxiv 2411.13768) = benim "kaydet→ZORLA eksik-halkası". 286 prose-ders tekrarı
   önlemedi → **O(n) context-dilution + knowing-doing-gap**; salience en düşük tam da self-verification fazında.
2. **Eval/gate = regression-floor** (lit: eval-driven-dev "write eval before build", TDD-ikizi; her caught-bug
   → deterministik-test; Anthropic/Braintrust/DeepEval) = Fable "incident→failing-repro" = benim "eval-corpus".
   **En güçlü yakınsama — merkezi mekanizma bu.**
3. **Gate blocking-statüsünü ÖLÇÜLMÜŞ-precision'da KAZANIR** (lit: "CI-rubber-stamp" failure-mode + threshold-
   against-variance, futureagi; alert-fatigue: 0.1%-FP×1M=1000-sahte, threshold-only recall'u 55pp-düşürür) =
   Fable "precision-telemetri + enforcement-ladder" = kullanıcının "çok-güvenlik-katmanı-friction" endişesi.
   **Sağlam-altyapı ≠ daha-çok-gate; iyi-kalibre gate.**

## 1. Plan (fazlı, dürüst: her madde artifact + efor + risk + sahip)

### Faz-0 — bu hafta, ucuz, en-yüksek-kaldıraç
- **[G1] Incident→failing-repro CI-gate** *(Fable#1 + eval-driven)*: her fix-PR bir repro-test-adı deklare eder;
  CI onu **merge-base SHA'da koşar→FAIL bekler**, head'de→PASS. Base'de-fail-etmeyen = sahte-doğrulayıcı.
  Bugünkü 6-vakanın ≥4'ünü (mock-maske/net-sıfır/cwd-naive) SINIF-olarak keser. *Artifact: ci.yml job.
  Efor: düşük. Risk: repro-yazımı-zorunlu (friction ama doğru-friction). Sahip: impl=surer, tasarım=klipper.*
- **[G2] Gate-telemetri tablosu** *(Fable#4 + alert-fatigue-lit)*: her gate-ateşleme `{gate, fired, outcome ∈
  {true_save, FP, overridden}, rationale}` → server.db. Kalibrasyon-temeli (bugün caught_by'ı elle-çıkardık →
  kalıcılaştır). *Efor: küçük tablo+helper. Risk: yok. Sahip: klipper.*

### Faz-1 — bir sprint
- **[G3] Her-caught-bug → eval-case** *(eval-corpus-lit + klipper)*: bugünkü bug'lar (netting/delivery-hook/cwd/
  spawn-collision) eval-gap2-desenine eklenir → asla-sessizce-regrese-etmez. *Efor: orta. Sahip: yakalayan-ajan.*
- **[G4] Entry-point registry + ∀-parametrize** *(Fable#2)*: cross-cutting özellik (delivery-filter/auth/guard)
  giriş-noktası-registry'sinden (API/hook/cron/MCP/WS) `parametrize` ile TÜM yollarda koşar. "tested-at-one-
  surface ≠ enforced-at-all-surfaces" = "mentions≠does"in ikizi; **delivery-gap sınıfını kökten keser.** *Efor:
  orta. Risk: registry-bakımı. Sahip: surer.*
- **[G5] Diff-scoped mutation** *(Fable#3 + mutation-lit)*: `diff-cover` (değişen-satır çalıştırılmalı) + değişen-
  dosyada `mutmut`; fix-revert-eden-mutant öldürülmeli. **Goodhart/spec-gaming panzehiri** (ci_fixer'da gördük).
  *Efor: orta (diff-scope=dakikalar). Risk: flaky-mutant. Sahip: surer.*

### Faz-2 — kalibrasyon + org-learning (over-mekanizasyon-freni)
- **[G6] Enforcement-ladder** *(Fable#4 + CI-rubber-stamp-lit)*: G2-telemetriden haftalık-rapor; precision-eşiği
  altı gate `blocking→warn→off`, üstü `warn→blocking`. Eşik **historical-variance'a göre** (zero-tolerance-değil).
  **Her FP de incident**: override→makine-okunur-gerekçe→otomatik-eval-case (aynı-FP ikinci-kez ateşleyemez).
  ACK-fatigue: gate-başına oturum-blocking-bütçesi, taşan→batched-digest. *Bu, soft-gate-pilotunun genellemesi.*
- **[G7] Memory-şema `enforcement_type`** *(Fable#6)*: `memories`'e `enforcement_type ∈ {eval,hook,lint,ci-gate,
  checklist,prose-only}` + `artifact_ref`. **Ölç: prose-only-tekrar-oranı vs mekanize-tekrar** → altyapıyı
  KENDİ-verisiyle gerekçelendirir (hipotez: mekanize→~0, prose-only-değişmez). Prose silinmez, artifact'ın
  provenance/rationale'i olur. *Efor: şema-migration. Sahip: klipper.*
- **[G8] Recurrence-taksonomisi** 286-feedback üzerinde: hangi-sınıf-kaç-kez → mekanizasyon-önceliği. *Ucuz,
  yüksek-içgörü. Sahip: klipper.*

### Faz-3 — çok-ajan: protokol→mekanizma
- **[G9] CLAIM→DB-lease** *(Fable#5)*: paylaşılan-DB'de `UNIQUE(resource)` atomik-INSERT + TTL + heartbeat; iş-
  scripti lease'siz-başlamayı reddeder. Uyum=DB-constraint (ajan-iradesi-değil). + atomik-ownership `UPDATE...SET
  owner=? WHERE owner IS NULL` + merge-queue (stale-base'i-öldürür, PR#252-sınıfı). *Bugün CLAIM 3-4× başarısız.*
- **[G10] Spawn-worktree-izolasyon** = PR#263 (zaten-tasarlandı). Protokol→mekanizma'nın somut-örneği.

## 2. Dürüst caveat'lar (mühendis-öz-kısıtı)
- **Dış-lit DOĞRULANMADI** (deep-research verify session-limit); Fable+grounded-veriyle-yakınsıyor ama re-verify
  gerekirse 22:50-sonrası. Prose-şişik-audit-riskine karşı: her G-maddesi somut-artifact'a bağlı, iddia-değil.
- **Over-mekanizasyon riski GERÇEK**: her gate bir Goodhart-yüzeyi açar. G5(mutation)+G6(precision-ladder) yerleşik-
  fren — **onlarsız gate ekleme.** "Her hatayı farklı-verifier-yakaladı" = Swiss-cheese-çalışıyor (başarısızlık-
  değil); hedef per-verifier-sınıf-yakalama-haritasını ÖLÇMEK + escape-rate'i-sıfıra-çekmek.
- **Self-serving-bias check**: bana-daha-AZ-bağımlı mekanik-gate önerdim (doğru-yön) ama sistem-içindeyim →
  surer+kullanıcı-çapraz şart.
- **surer-akışı bekliyor**: spec-verify-sistematikleştirme + shared-state-domain gelince G1/G9 güncellenir.

## 3. Öneri (dürüst öncelik)
G1+G2 **bu hafta** (CI-yaml + tablo, en-yüksek-ROI, düşük-risk). G3-G5 sprint. G6-G8 kalibrasyon (over-mek-freni,
G1-sonrası). G9-G10 mevcut-atomik-UPDATE + PR#263'ün-genellemesi. **P1-a DB-merkezi ile ÇAKIŞMAZ** (o ayrı-iş,
bu meta-altyapı) — sıralama Turgut. Başlangıç için G1 tek-başına 6-vakanın-yarısını-kapatır = en-dürüst-ilk-adım.
