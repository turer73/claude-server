#!/usr/bin/env bash
# G1 repro-gate — "fix-remove -> test-MUST-fail" mekanik-gate (öğrenme-altyapısı planı, docs/learning-infrastructure-plan.md).
#
# PROBLEM: prose-ders "gerçek-üretim-yolunu test et" BELİRSİZ (yorumlanır, atlanır) -> mock-maske/
# net-sıfır/absence sınıfı testler bug'ı yakalamadan yeşil-verir (bu oturumda 6× tekrarladı).
# ÇÖZÜM (birleştirici-mekanik, surer #100353): iddiayı TERSİNE-çalıştır. Declared repro-test'i
# merge-BASE'de (fix YOK, sadece test) koş -> FAIL/ERROR bekle. Base'de PASS = test bug'ı YAKALAMIYOR
# = sahte-doğrulayıcı. İkili-kesin: ya-base'de-fail-eder (gerçek) ya-etmez (yalan).
#
# DÜRÜST SINIR (surer #100356 push-back-1): G1 "test BİR-ŞEY yakalıyor mu" (no-op-değil) garantiler
# AMA "test DOĞRU-YOLU (prod-caller-akışını) modelliyor mu" DEĞİL. Yanlış-yolu-test-eden repro de
# base'de-fail-edebilir (cwd-naive-fix: git-izolasyonu-test-etti, gerçek-yazma-yolunu-değil ->
# G1-yeşil-ama-bug-var). G1 no-op-test-sınıfını keser; yanlış-yol-test-sınıfı G4(entry-point-registry,
# repro GERÇEK-caller'dan-geçmeli) ile kapanır. **G1+G4 = çekirdek-çift, G1-yalnız-YETMEZ.**
#
# Ortam değişkenleri: PR_BODY, BASE_SHA, HEAD_SHA. Gerekli: fetch-depth:0 (base+head erişimi).
set -uo pipefail

PR_BODY="${PR_BODY:-}"
BASE_SHA="${BASE_SHA:-}"
HEAD_SHA="${HEAD_SHA:-}"

fail() { echo "::error::$*"; exit 1; }

# 1) 'Repro-Test:' satırını çek (case-insensitive, satır-başı; PR-template zorunlu-alanı)
line=$(printf '%s\n' "$PR_BODY" | grep -ioP '^\s*Repro-Test:\s*\K.+' | head -1 | sed 's/[[:space:]]*$//' | tr -d '\r')
[ -z "$line" ] && fail "PR body'de 'Repro-Test:' satırı YOK (zorunlu, .github/pull_request_template.md). Fix değilse: 'Repro-Test: N/A — <neden>'."

# 2) N/A -> skip (docs/refactor/infra; davranış-değişmez)
if printf '%s' "$line" | grep -qiE '^N/?A\b'; then
  echo "Repro-Test: N/A ($line) — repro-gate atlandı (fix-dışı PR)."
  exit 0
fi

# 3) test-id parse (tests/x.py::test; dosya = ilk '::'e kadar)
TEST_ID="$line"
TEST_FILE="${TEST_ID%%::*}"
case "$TEST_FILE" in
  tests/*) : ;;
  *) fail "Repro-Test tests/ altında olmalı: '$TEST_FILE' (parse: '$line')";;
esac
[ -n "$BASE_SHA" ] && [ -n "$HEAD_SHA" ] || fail "BASE_SHA/HEAD_SHA boş (PR-context + fetch-depth:0 gerekli)."
git cat-file -e "$HEAD_SHA:$TEST_FILE" 2>/dev/null || fail "Repro-Test dosyası head'de yok: $TEST_FILE"

echo "Repro-Test = $TEST_ID | base=${BASE_SHA:0:9} head=${HEAD_SHA:0:9}"

# 4) BASE'e geç + SADECE test-dosyasını head'den overlay et (kaynak-fix'i DEĞİL)
git checkout -q "$BASE_SHA" 2>/dev/null || fail "base checkout başarısız ($BASE_SHA)."
git checkout -q "$HEAD_SHA" -- "$TEST_FILE" || fail "test-dosyası overlay başarısız ($TEST_FILE)."

# 5) Testi koş: FAIL/ERROR bekle (base'de bug var -> gerçek-test yakalamalı)
#    NOT: ERROR (import/fixture) da 'non-pass' = kabul (test fix'siz koşamıyorsa fix yine gerekli).
if python -m pytest "$TEST_ID" -q -p no:cacheprovider >/tmp/repro.log 2>&1; then
  echo "----- repro.log (son 25 satır) -----"; tail -25 /tmp/repro.log
  fail "Repro-test base'de PASS etti -> test bug'ı YAKALAMIYOR (SAHTE-doğrulayıcı). fix-geri-alınca FAIL etmeli. Kontrol: mock-sabit mi? net-sıfır mı? gerçek-üretim-yolu mu?"
fi
echo "✓ Repro-test base'de FAIL/ERROR (bug'ı yakalıyor) -> head'de PASS = normal-CI. Doğrulayıcı GERÇEK."
