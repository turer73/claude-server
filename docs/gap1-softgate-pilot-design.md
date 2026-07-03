# GAP-1 Soft-Gate Pilot — Tasarım (ci_fixer Kapsam-1, #1224 #1/#6)

> **Lane:** tasarım = klipper (bu doküman) · implementasyon = surer.
> **Kapsam:** SADECE ci_fixer soft-gate pilotu (Kapsam-1). Policy-gate (#1222 dispatch DENY/HOLD)
> AYRI ve sonra. Soft-gate'i AÇMA kararı (`ACTION_REVIEW_SOFT_GATE=ON`) = **kullanıcı**; bu iş sadece
> mekaniği doğru+güvenli hale getirir, flip'i yapmaz.

## 0. Neden pilot
Eval-gate GEÇTİ (PR#253: action_review catch=1.0/fb=0.0, eşik ≥0.90/≤0.05). Detector artık
evasion-yüzeyini kapsıyor → soft-gate teknik olarak güvenle açılabilir. Ama açmadan önce iki
mekanik-defekt (#1/#6) düzeltilmeli, yoksa AÇILINCA yanlış davranır.

## 1. Mevcut durum (app/core/ci_fixer.py)
- `_review_fix_diff` (satır 82): attempt-delta diff'i tarar; `gate_on` → `True`=blok, `False`=notify.
  DEFAULT-OFF (fail-safe zaten var: tarama-hatası→fail-open notify, satır 112).
- `attempt_fix` loop (satır 457+): her attempt'te `_review_fix_diff` PRE-test çağrılır (526, doğru),
  testler koşulur (529), `record_lesson` (536), testler geçer + `held_for_review` → held-return (558).

## 2. Defekt #6 — held-fix `outcome="passed"` etiketleniyor (POISON)
**Sorun:** satır 532 `outcome = "passed" if failed==0 else "failed"`, `record_lesson` (536) held-check'ten
(558) ÖNCE çağrılıyor. Şüpheli-ama-testler-geçen (held) fix, lessons-DB'ye **"passed"** yazılıyor →
gelecek ci_fixer bu spec-gaming stratejisini "başarılı" örnek olarak öğrenir (self-poison).

**Fix spec:**
- `outcome`, held durumunda `"held"` (yeni değer) olmalı, `"passed"` DEĞİL.
- Hesap: `held_for_review` testlerden ÖNCE (526) belli; `record_lesson`'a geçen outcome:
  ```
  outcome = "held" if (held_for_review and test_result.failed == 0) else ("passed" if failed==0 else "failed")
  ```
- `record_lesson` / ci_signal_dedup şemasında `outcome` serbest-metin mi enum mu KONTROL et; enum ise
  `"held"` ekle (migration gerekmez, TEXT ise). Held-lesson `context_lessons` promptuna "passed" örnek
  olarak SIZMAMALI (build_fix_prompt geçmiş-özet filtresi held'i "başarısız/şüpheli" muamelesi görsün).
- Test: held-attempt sonrası DB satırı `outcome='held'`; sonraki-run prompt'unda o diff "passed" örneği değil.

## 3. Defekt #1 — retry-persistence (laundering) + hold-timing
**Sorun:** `held_for_review` attempt-LOCAL, attempt-delta üzerinden. Senaryo:
1. attempt-1: assertion zayıflatır (şüpheli) AMA testler FAIL → held-return TETİKLENMEZ (558 sadece pass'te),
   loop RETRY'ye devam. Working-tree'de attempt-1 değişikliği KALIR.
2. attempt-2: temiz gerçek-fix ekler, testler PASS. `_review_fix_diff` attempt-2 delta'sını tarar →
   attempt-1'in zayıflatmasını GÖRMEZ → held=False → **accept**. Kümülatif tree'de attempt-1 spec-gaming var.

**Fix spec (kümülatif accept-time review):**
- İki tür review tut:
  - **Per-attempt** (mevcut 526): erken-uyarı emit (notify), değişmez.
  - **Accept-time kümülatif** (YENİ): testler geçince (555), accept'ten ÖNCE, LOOP-BAŞI baseline'dan
    (attempt-1 öncesi snapshot, `run_baseline_ref`) bu-ana kümülatif diff'i tekrar tara. Şüpheli → held.
- `attempt_fix` başında `run_baseline_ref, run_pre_untracked = await _snapshot_baseline(cwd)` bir kez al
  (loop-dışı). Accept-noktasında `scan_ci_fixer_diff(cumulative_diff, ...)` → held ise held-return.
- Alternatif (daha basit, kabul edilebilir): `run_held = run_held or held_for_review` sticky-flag; ama
  bu attempt-1-FAIL-attempt-2-PASS laundering'i YAKALAMAZ (attempt-1 held=False'du çünkü fail'de
  değerlendirilmedi). **Kümülatif-diff yaklaşımı ŞART** — sticky-flag yetmez.
- Hold-timing: kümülatif review accept-KAPISINDA (test-geçti dalında, 555-558) olmalı; per-attempt erken-uyarı korunur.
- Test: attempt-1 assertion-drop+FAIL, attempt-2 trivial-pass → accept-time kümülatif review held=True.

## 3.1 Final implementasyon (MERGED: PR#255 `39ca793` + PR#256 `a1059f8`, 7 Codex-tur / 8 P2 / 0 FP)
> Bu bölüm gerçekleşen tasarımı kaydeder — yukarıdaki §1/§3 spec BAŞLANGIÇ noktasıydı, Codex
> iterasyonuyla önemli ölçüde değişti. **DÜZELTME:** §3'ün "run-başı tek-diff ŞART, sticky yetmez"
> çıkarımı ESKİ koda (held yalnız pass-dalında) aitti; final tasarım bunu tersine çevirdi.

- **`_review_fix_diff` → ikiye ayrıldı** (Codex r2): `_scan_fix_diff` (capture+tara, emit YOK) +
  `_apply_review_verdict` (şüpheli-emit + gate-karar). Kümülatif tarama TESTLERDEN ÖNCE (adım 3.6);
  verdict pass-dalında (4.5) — runner-artifact (snapshot/golden/coverage) kümülatif diff'i
  kirletmesin (post-test capture = sahte-supheli → shadow-sinyali bozar).
- **Run-başı-baseline KALKTI → kümülatif = pre-test attempt-delta'ların BİRLEŞİMİ (union)** (Codex r4):
  run-başı tek-diff, retry-arası runner-değişikliğini Claude-değişikliğinden YAPISAL ayıramıyordu.
  Her attempt-baseline önceki koşunun artifact'ını yutar → union runner-temiz, laundering korunur.
- **Netting fix = sticky per-attempt OR + concat** (Codex r5-a): concat-scan tek-başına yetmez —
  att-1 assertion-siler + att-2 aynı dosyaya assertion-ekler → `removed-added` NET-SIFIR, drop kaybolur.
  Kümülatif verdict = concat-scan VEYA önceki-attempt pre-test-scan şüpheli (`attemptN:` önekli sinyal).
  (§3'ün "sticky yetmez"i artık geçersiz: pre-test per-attempt scan sticky HEM yeterli HEM netting-bağışık.)
- **Held-dedup** (Codex r5-b): `get_recent_occurrences` artık `outcome IN ('failed','held')` — held
  "çözülmemiş" sayılır, yoksa gate-ON tekrarlı-held'de #6 "ÖRNEK ALMA" uyarısı hiç yüzeye çıkmazdı.
- **SHA-pin** (Codex r3): `_snapshot_baseline` clean-tree'de literal "HEAD" (hareketli-ref) döndürüyordu;
  ci_fixer git-checkout'a izinli → run-ortası HEAD kayarsa launder gizlenir. Somut SHA'ya pin.
- **Error-attempt capture (571)** (Codex r6): timeout/parse-hatası veren Claude çağrısı working-tree'yi
  zaten zayıflatmış olabilir; capture+scan+sticky artık hata-dalında da koşar (yoksa sonraki attempt
  baseline'ına "temiz" girer = launder). Şüpheli-diff insan-gözüne (emit-on-error).
- **Untracked-weakening (574)** (Codex r6 + **klipper prod-leak yakalama**): run-içi oluşan untracked
  dosya sonraki attempt'te zayıflatılırsa izlenmeli. `run_untracked_content` cache + difflib-diff
  (silme '-' görünür). **Guard kritik:** `(f in run_untracked_content or f not in pre_untracked)` —
  ilk fix (`f not in pre_untracked`) prod'da LEAK ediyordu (att-1 dosyası att-2'nin per-attempt
  pre_untracked'ine girip dışlanıyordu; test `set()` ile maskelemişti). Ampirik-repro + fix-yok→test-FAIL
  ile doğrulandı.

## 4. Rollout / kill-switch / fail-safe
- **Enable kriteri (operasyonel):** `make eval-gap2` → action_review catch≥0.90 & fb≤0.05 GÖSTERMELİ
  (PR#253 sonrası sağlanıyor). Bu doğrulanmadan flip YAPILMAZ.
- **Kill-switch:** `ACTION_REVIEW_SOFT_GATE` `read_env_var` ile (systemd .env, #174 config-gate deseni —
  os.environ.get TEK-BAŞINA serviste kill-switch'i görmez; `soft_gate_enabled()` zaten read_env_var).
  OFF/yok/eval-geçmemiş → notify-only. **Kanıtla:** flip sonrası process-env + canlı-test (config-gate dersi).
- **Fail-safe:** tarama-exception → fail-open notify (mevcut, satır 112). Kümülatif-review exception de
  fail-open olmalı (accept'i bloklamaz, sadece notify) — gate-hatası ci_fixer'ı kilitlemesin.
- **Staged:** (a) mekanik-fix'ler merge (SOFT_GATE hâlâ OFF), (b) SHADOW gözlem: OFF'ta emit'lenen
  "supheli"/"held-would-be" event'lerini 1 hafta izle (FP var mı), (c) kullanıcı-onayıyla ON.
- **Geri-alma:** `ACTION_REVIEW_SOFT_GATE=0` + restart → anında notify-only.

## 5. Kapsam-DIŞI
- Policy-gate (#1222 dispatch DENY/HOLD) — AYRI tasarım, sonra.
- Soft-gate flip'in kendisi (OFF→ON) — kullanıcı kararı, bu iş değil.
- LLM tabanlı review — yok (GAP-2 kanıtı: deterministik-desen fail-safe).

## 6. Kabul kriterleri (surer impl)
1. Held-fix `outcome='held'` (poison yok) + geçmiş-özet filtreli. Unit-test.
2. Accept-time kümülatif review (laundering yakalanır). Unit-test (attempt-1-fail-drop + attempt-2-pass).
3. Kümülatif-review fail-safe (exception→notify, accept-blok yok). Unit-test.
4. `make eval-gap2` hâlâ catch=1.0/fb=0.0 (regresyon yok).
5. SOFT_GATE DEFAULT-OFF korunur; tüm mevcut ci_fixer testleri yeşil.
6. Codex-review temiz (safety-critical); CLAIM-protokolü (paralel-çakışma).

**İlgili:** [[project_gap1_faz2_eval_harden_2026_07_02]], docs/gap1-faz2-hardening-eval-gate-design.md,
bug #1224 (#1/#6 kalan), #1222 (policy-gate, sonra).
