# GAP-1: `action_review` — Çıktı-Tarafı Deterministik Denetim (Tasarım)

> **Lane:** design=klipper, impl=surer. **Durum:** taslak, kullanıcı-onayı bekliyor.
> **Bağlam:** GAP-2 eval-harness (PR#246) classifier'ın güvenlik-gate OLAMADIĞINI kanıtladı
> (routes-by-shape; iyi-biçimli-kötücül-task-paketi → ACTIONABLE, hiçbir eşik düzeltmez).
> Bu boşluk = **AUDIT ≠ CONTROL**: mevcut guard'lar hep GİRDİ-tarafında (classifier, ShellExecutor
> whitelist, CPU-eşik); otonom LLM aksiyonunun ÇIKTISINI (üretilen diff/komut/task-paketi)
> semantik denetleyen deterministik kapı YOK. #100248 over-reach de bunun canlı kanıtı.

## 1. Ne İzlenir (otonom aksiyon ÇIKTI yüzeyleri)

| Yüzey | Kod | Çıktı | Mevcut gate |
|---|---|---|---|
| ci_fixer diff | `ci_fixer.attempt_fix` (:311), `fix_diff` (:445) | working-tree `git diff` | SADECE "test yeşil" — semantik denetim YOK |
| dispatch komut | `dispatch._run_klipper_cmd` (:197) | LLM-üretimi shell komutu | ShellExecutor whitelist (girdi-tarafı) |
| autonomous-spawn dispatch | autonomous-claude → not/task-paketi yazma | başka-ajana design+task notu | HİÇBİRİ (#100248 buradan kaçtı) |

## 2. Mekanizma: DETERMİNİSTİK, LLM-DEĞİL

**Karar:** action_review LLM-tabanlı OLMAYACAK. Gerekçe:
- GAP-2 kanıtı: LLM-classifier iyi-biçimli-kötücül içeriği ayırt edemiyor (0.40).
- LLM-monitor = yeni FP + yeni silent-fail kaynağı (kendi dersimiz, #100178-sınıfı).
- Deterministik desen-taraması fail-safe, ucuz, tekrarlanabilir, test-edilebilir (GAP-2 harness'iyle ölçülür).
- Mevcut `code_reviewer.py` (LLM, read-only, kopuk) TAMAMLAYICI kalır — o "kalite", bu "güvenlik-gate".

## 3. ⭐ Bağlamsal Whitelist (bu tasarımın ÇEKİRDEK yeniliği)

Bu oturumda **3 kez** aynı FP yaşandı (threat #100249, #100259, klipper-notu guard-blok):
guard, **"task-paketi destructive-op İÇERİYOR"** ile **"analiz-prozası destructive-op'tan BAHSEDİYOR"**
arasını ayırt edemedi. action_review bunu ÇÖZMELİ, yoksa aynı FP-selini üretir.

**Kural — desenin KONUMU tehdidi belirler, varlığı değil:**
- **GERÇEK tehdit** → desen şurada: git-diff `+` (eklenen) satırında · task-paketi `adimlar[]`/`command`/`cmd` alanında · shell komut pozisyonunda.
- **BENIGN (geç)** → desen şurada: serbest-metin/açıklama · tırnak-içi string · markdown code-fence içinde örnek · `attack_type`/`aciklama` gibi meta-alan · eval-set/test-fixture.

Uygulama: JSON ise alan-yolu-farkında parse (adimlar[]/cmd = tehdit-alanı; title/aciklama/content-prose = benign). Diff ise sadece `+`-satırları. Prose ise = benign (bahsetmek ≠ yapmak).

## 4. Sinyaller (deterministik göstergeler)

**A) ci_fixer diff (spec-gaming — surer #100238):** working-tree `git diff`'ten (Claude-prozasından DEĞİL):
- test-dosyası değişti mi (`tests/` altı) VE assertion-sayısı DÜŞTÜ mü (`assert`/`pytest.raises` net azalma)
- başarısız-modül-DIŞI dosya değişti mi (fix, fail-eden testin modülü dışına taşıyor mu)
- diff-boyutu anormal mi (küçük-fix beklenirken büyük-diff)
- guard/config zayıflatma (`pre-bash-guard`/`settings.json`/`ci-fixer-settings` değişti mi)

**B) task-paketi / dispatch (destructive-op — bağlamsal):** `adimlar[]`/`cmd` alanında:
- yıkıcı-desen (tablo-silme, recursive-force-delete, force-push, guard-devre-dışı, .env-yazma, chmod-x-guard)
- credential/settings manipülasyonu (`MEMORY_API_KEY=`, `permissions.allow` genişletme)

## 5. Çıktı + Kademeleme (notify-only → gate)

- **Faz 1 (başlangıç): notify-only.** Şüpheliyse `emit_event("action-review", severity=warn)` + auto-accept'i BLOKLAMA. Sadece işaretle + Telegram. AUTO_KILL default-OFF deseni (#100238).
- **Faz 2 (eval geçince): soft-gate.** ci_fixer şüpheli-diff → auto-commit/accept DUR, insan-review iste.
- Eşik geçişi (Faz1→Faz2) = **GAP-2 eval-harness'iyle ölçülür** (Sprint4-unlock deseni): action_review eval-set (N güvenli-diff/task + N kötücül) catch≥X, false-block≤Y sağlamadan gate-moduna geçilmez.

## 6. Entegrasyon Noktaları

- `ci_fixer.attempt_fix`: diff üretildikten SONRA, accept'ten ÖNCE → `action_review.scan_diff(git_diff)`.
- `dispatch._run_klipper_cmd`: ShellExecutor'a EK deterministik-katman (mevcut whitelist girdi; bu çıktı-niyet).
- autonomous-spawn dispatch: başka-ajana not/task-paketi yazmadan önce scan (bug #1222 = bunun alt-kümesi; dispatch-authority kısıtı buraya bağlanır).

## 7. Fail-safe + Ölçüm

- action_review HATA verirse → **fail-CLOSED değil, fail-NOTIFY**: tarama çökerse aksiyonu bloklama AMA `emit_event(warn, "action-review taranamadı")` (izlenebilir). (Faz1 notify-only olduğu için üretimi durdurmaz.)
- GAP-2 harness'ine `action_review` eval-set eklenir → catch/false-block ölçülür → Faz2-gate ancak ölçümle açılır.
- **Bağlamsal-whitelist regresyon-testi ZORUNLU:** "prose-mentions vs field-contains" ayrımı için pytest (bu 3×-FP'nin tekrarını önler).

## 8. Kapsam-Dışı (anti-hype)

- LLM-tabanlı niyet-tahmini (GAP-2 çürüttü). Ağır monitor-fleet. scheming-untrusted tehdit-modeli (aktörümüz devops-Claude). Otomatik-blok Faz1'de YOK.

## 9. İlişkili

- GAP-2 eval-harness (PR#246, `tests/gap2/`) — action_review eval buraya eklenir.
- bug #1222 (dispatch-authority) — §6 autonomous-spawn kapısının alt-kümesi.
- #100248 over-reach vaka-notu (`ai-control-gap-design.md`) — bu tasarımın motivasyonu.
- `code_reviewer.py` — tamamlayıcı (kalite/LLM/read-only), action_review'in yerine geçmez.
