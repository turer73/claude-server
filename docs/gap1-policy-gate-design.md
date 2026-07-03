# GAP-1 Policy-Gate — Tasarım (cross-agent dispatch DENY/HOLD, #1222)

> **Lane:** tasarım = klipper (bu doküman) · implementasyon = surer.
> **Kapsam:** cross-agent DISPATCH enforcement (#1222). Soft-gate (ci_fixer çıktı-denetimi) AYRI ve
> BİTTİ (PR#255+#256, `a1059f8`). Policy-gate'i AÇMA kararı (`DISPATCH_POLICY_GATE=ON`) = **kullanıcı**;
> bu iş mekaniği doğru+güvenli hale getirir, flip'i YAPMAZ.
> **Ön-koşul (HAZIR):** origin-tag attribution unforgeable (A-2, PR#249) — gate bunun üstüne kurulur.

## 0. Neden policy-gate
#1222: **otonom-claude spawn'ın cross-agent DISPATCH yetkisi insan-onayını atlıyor.** Bir otonom ajan
(`klipper-autonomous`) başka ajana/cihaza consequential (sonuç-doğuran) iş-paketi dispatch edebiliyor —
insan-gate yok. Detector (`scan_dispatch_note`) bunu TESPİT ediyor ama ENGELLEMİYOR.

Soft-gate (ci_fixer) tamamlandı; aynı desen (`scan → gate_enabled() → verdict`) dispatch tarafına
uygulanır. Fark: ci_fixer'da verdict=held (fix accept-etme); burada verdict=HOLD (dispatch teslim-etme →
insan-onayı).

## 1. Mevcut durum
- **Detector VAR:** `scan_dispatch_note` (`app/core/action_review.py:516-547`), deterministik (LLM-değil).
  İki sinyal: (a) `autonomous_cross_agent_dispatch` = `_is_autonomous_origin(from_device)` (satır 496-498)
  **VE** cross-agent (`to_device` dolu veya content-zarfında alıcı) **VE** JSON task-paketi — **#1222'nin
  tam hedefi**. (b) `dispatch_destructive_op` = yalnız executable-alanlarda (`_EXEC_KEYS`) yıkıcı-desen
  (alan-farkında whitelist, prose/nested-data FP değil).
- **Enforcement YOK:** `_review_dispatch_note` (`app/api/memory/notes.py:24-55`) **notify-only + FAIL-OPEN**;
  üstelik `create_note` (satır 79-150) notu **INSERT+commit ETTİKTEN SONRA** (satır 127-131) review çağırıyor
  (satır 134). Yani suspicious dispatch tespit ediliyor ama not zaten yazıldı+teslim-edilebilir.
- **Teslim yolu:** `list_notes` (satır 58-76) + Stop-hook inbox → `WHERE to_device=? ... unread`. `status`
  kolonu YOK (notes şeması: from_device/to_device/title/content/read/read_by).
- **Ön-koşul hazır:** `dispatch_origin` (`memory/__init__.py:60-64`) otonom-key ise `from_device`'ı
  sunucu-tarafında `klipper-autonomous`'a ZORLAR (unforgeable); body-claim yok sayılır. Gate buna güvenir.

## 2. Consequential-dispatch seti (neyi gate'liyoruz)
1. **`create_note` cross-agent note** (`notes.py`) — otonom spawn'ın allowlist ile eriştiği ANA kanal
   (`autonomous-claude-settings.json`: `Bash(curl POST .../memory/*)`). **Gate BURAYA konur.**
2. **`dispatch.py::_send_to_surer`** — kendi içinde `create_note`'a POST eder (to_device SET etmez, alıcıyı
   content-zarfında taşır) → #1 gate'i bunu da OTOMATİK kapsar.
3. **`dispatch.py` KLIPPER shell-exec** (`_run_klipper_cmd`) — AYRI yüzey; ShellExecutor whitelist +
   interpreter-deny zaten var (guardrail). **Bu iş kapsamı DIŞI** (not-kanalı değil, ayrı denetim; ileride).

→ Tek hook = **`create_note`**. #1 ve #2 aynı DB-notes yüzeyinde birleşir.

## 3. Gate mekaniği
### 3.1 Kime uygulanır — YALNIZ otonom-origin
Gate **`_is_autonomous_origin(from_device)`** ile anahtarlanır (sunucu-set, unforgeable). Interaktif
`klipper`/`surer` (= insan operatörü) **ASLA gate'lenmez** — #1222 tam da "otonom insan-gate'i atlıyor"
sorunu. from_device forge-edilemez (A-2), o yüzden gate baypas-edilemez.

### 3.2 Hangi sinyaller enforce edilir
- `autonomous_cross_agent_dispatch` → **HOLD** (#1222 özü: otonom-consequential-dispatch insan-onayı bekler).
- `dispatch_destructive_op` (otonom-origin'den) → **HOLD** (kritik-severity). **AÇIK-KARAR (§8):** bunu
  DENY'ye eskale etmek (yıkıcı-desen hiç yazılmasın) opsiyonu — pilot HOLD ile başlar (auditable).

### 3.3 Verdict: HOLD (DENY değil — §8 gerekçe)
`_review_dispatch_note` notify-only'dan **verdict-dönen** hale getirilir (ci_fixer `_apply_review_verdict`
deseni): `(status, suspicious, signals)`. `create_note` INSERT'i bu status ile yapar:
```
gate_on = dispatch_policy_gate_enabled()
scan = scan_dispatch_note(content, from_device, to_device)   # INSERT'ten ÖNCE
status = "held" if (gate_on and scan["suspicious"] and _is_autonomous_origin(from_device)) else "active"
# INSERT ... status=?  (yeni kolon)
```
- **gate-OFF** (shadow/default): status='active' + warn-emit (mevcut notify-only davranış korunur).
- **gate-ON + suspicious + autonomous:** status='held' + **critical-emit** + insan-yükseltme (§4).

### 3.4 Env-gate + fail-safe
- **`DISPATCH_POLICY_GATE`** — soft-gate'ten AYRI env-flag, **DEFAULT-OFF**, `read_env_var` ile
  (config-gate dersi #174: `os.environ.get` systemd .env'i görmez). `dispatch_policy_gate_enabled()`
  = `soft_gate_enabled()` kopyası, ayrı-key.
- **FAIL-OPEN korunur:** scan-exception → status='active' + warn-emit, not TESLİM edilir. Koordinasyon-kanalı
  omurga; gate-bug'ı kanalı BRICKLEMEZ. (Soft-gate pilot-tasarım-4 ile aynı ilke.)

## 4. HOLD yaşam-döngüsü (insan-onayı = restore edilen human-gate)
- **Şema:** `notes.status TEXT DEFAULT 'active'` (migration, geri-uyumlu; değerler: `active`/`held`/`rejected`).
  Mevcut satırlar NULL→'active' muamelesi.
- **Teslim-filtresi:** `list_notes` + Stop-hook inbox unread-sorgusuna `AND COALESCE(status,'active')='active'`
  → held notlar ALICIYA teslim EDİLMEZ (dispatch etkisiz-kalır ta ki onaylanana dek).
- **İnsan-yükseltme (onay kanalı — session-start + dashboard):** held dispatch'ler kullanıcıya:
  (a) session-start hook: "N held dispatch onay bekliyor" satırı, (b) dashboard'da liste (from→to, sinyaller,
  content-önizleme). Telegram DEĞİL (flood-riski #100224; kalıcı-liste yeterli, aciliyet-yok).
- **Onay endpoint'leri (MASTER-key zorunlu — `verify_master_key`, otonom-key REDDEDİLİR):**
  - `PUT /notes/{id}/approve` → status='active' → not teslim-edilebilir (release).
  - `PUT /notes/{id}/reject` → status='rejected' → kalıcı-held (audit-kaydı durur, teslim yok).
  - **Neden master-key:** otonom-ajan kendi held-dispatch'ini onaylayamasın (self-approval baypası;
    `verify_master_key` deseni onboarding/key-sızan route'lardaki gibi).

## 5. Rollout / kill-switch / fail-safe
- **Enable kriteri (operasyonel):** `make eval-gap2` → Part D `scan_dispatch_note` catch≥0.90 & fb≤0.05
  GÖSTERMELİ (ŞU AN 1.0/0.0 — sağlanıyor). Doğrulanmadan flip YOK.
- **Staged:** (a) mekanik merge (`DISPATCH_POLICY_GATE` OFF), (b) SHADOW ~1hafta: OFF'ta kaç dispatch
  HOLD-edilir**di** (payload'da `would_hold=true`) izle — gerçek otonom-consequential mi, FP mi
  (interaktif-yanlış-tag / benign task-paketi). (c) kullanıcı-onayıyla ON.
- **Kill-switch:** `DISPATCH_POLICY_GATE=0` + restart → anında notify-only. Held-kalan notlar için
  migration-geri-al gereksiz (status='active' default; sadece yeni HOLD durur).
- **Fail-safe:** §3.4 fail-open. EK: onay-endpoint'i çökerse held notlar birikir ama TESLİM edilmez
  (güvenli-taraf); operatör manuel `UPDATE status='active'` ile release edebilir.

## 6. Kapsam-DIŞI
- `dispatch.py` KLIPPER shell-exec gate'i (ayrı yüzey; ShellExecutor whitelist zaten var) — ileride.
- Soft-gate flip'in kendisi + policy-gate flip'in kendisi — kullanıcı kararı.
- LLM-tabanlı dispatch-review — yok (deterministik-desen fail-safe, GAP-2 dersi).
- Origin-tag mekaniği — zaten enforce (A-2), değişmez.

## 7. Kabul kriterleri (surer impl)
1. `notes.status` kolonu (migration, default 'active', geri-uyum testli). Unit-test.
2. `create_note` scan-before-insert; gate-ON+suspicious+autonomous → status='held' (teslim yok). Unit-test.
3. Teslim-filtresi: held not `list_notes`/inbox'ta ALICIYA görünmez; MASTER'a görünür. Unit-test.
4. `approve`/`reject` endpoint (MASTER-key; otonom-key 403). Self-approval baypası testli. Unit-test.
5. **Interaktif-origin ASLA held** (yalnız otonom). Unit-test (from_device='klipper' → active).
6. Fail-open: scan-exception → status='active' + warn (kanal bricklenmez). Unit-test.
7. `DISPATCH_POLICY_GATE` DEFAULT-OFF; OFF'ta davranış = mevcut notify-only (regresyon-yok).
8. `make eval-gap2` Part D hâlâ catch=1.0/fb=0.0.
9. Codex-review temiz (safety-critical → Opus x2); CLAIM-protokolü.

## 8. Açık kararlar (kullanıcı — flip-öncesi)
- **DENY vs HOLD default:** HOLD önerildi (auditable, silent-fail-yok, geri-dönülebilir). Kullanıcı-onayı
  bekliyor (surer #100306 "default DENY?" sordu; klipper-cevap: HOLD, gerekçe §3.3).
- **Yıkıcı-op eskalasyonu:** `dispatch_destructive_op` → DENY (hard) mi HOLD-critical mi? Pilot HOLD;
  eskalasyon flip-sonrası değerlendirilir.
- **Onay kanalı:** session-start+dashboard önerildi (Telegram-flood-riski yok). Kullanıcı-onayı bekliyor.
- **Flip (OFF→ON):** kullanıcı, shadow-hafta sonrası.

**İlgili:** [[project_session_2026_07_03_softgate_merge_honest]], docs/gap1-softgate-pilot-design.md,
docs/gap1-action-review-design.md, bug #1222, feedback #1117 (unforgeable enforcement),
[[feedback_pattern_match_contains_vs_mentions]] (executable-alan whitelist = FP-önleme).
