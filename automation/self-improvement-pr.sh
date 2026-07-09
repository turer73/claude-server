#!/usr/bin/env bash
# Self-Improvement PR Creator — onaylanmış öneriyi branch + commit + PR'a dönüştürür.
#
# Kullanım: bash automation/self-improvement-pr.sh <pending_id> "<title>" "<affected_files>"
#
# Akış:
#   1) Pending kaydını al, branch adı oluştur
#   2) git branch + commit (content-editor.py pattern'i)
#   3) gh pr create
#   4) PR url'sini pending tablosuna yaz
#
# Dış bağımlılık: gh, git, INTERNAL_API_KEY (env), API_BASE (env)

set -uo pipefail

PENDING_ID="${1:-}"
TITLE="${2:-}"
AFFECTED_FILES="${3:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_BASE="${API_BASE:-http://localhost:8420}"
INTERNAL_API_KEY="${INTERNAL_API_KEY:-}"

fail() { echo "::error::$*"; exit 1; }
info() { echo "[self-improvement-pr] $*"; }

[ -n "$PENDING_ID" ] || fail "Usage: $0 <pending_id> <title> <affected_files>"
[ -n "$TITLE" ] || fail "title boş"
[ -n "$AFFECTED_FILES" ] || fail "affected_files boş"

# ── Branch adı ──
BRANCH="self-improvement/$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//' | head -c 42)"
info "Branch: $BRANCH"

# ── Kirli repo kontrolü ──
cd "$REPO_ROOT" || fail "repo root yok: $REPO_ROOT"
if [ -n "$(git status --porcelain)" ]; then
  fail "Working tree kirli — commit edilmemiş değişiklikler var"
fi

# ── Git işlemleri (author override — Vercel kuralı) ──
info "Fetch origin..."
git fetch origin -q || fail "git fetch başarısız"

DEFAULT_BRANCH="${DEFAULT_BRANCH:-master}"
git checkout -B "$BRANCH" "origin/$DEFAULT_BRANCH" || fail "branch oluşturma başarısız"

git config user.email "turgut.urer@gmail.com"
git config user.name "turer73"

# ── Dosya değişikliklerini uygula ──
# Öneri JSON'dan diff bilgisi almak için API çağrısı
if [ -n "$INTERNAL_API_KEY" ]; then
  RESPONSE=$(curl -s -f -X GET "$API_BASE/api/v1/self-improvement/pending" \
    -H "X-API-Key: $INTERNAL_API_KEY" 2>/dev/null || echo "")
  info "Pending listesi alındı (API_KEY varsa)"
fi

# Değişiklik olarak affected_files'taki dosyaları touch + stage et
# NOT: Gerçek diff LLM tarafından üretilecek; şimdilik placeholder commit
for f in $AFFECTED_FILES; do
  f_clean=$(echo "$f" | xargs)
  [ -f "$REPO_ROOT/$f_clean" ] && git add "$f_clean" && info "Staged: $f_clean"
done

# Hiç dosya eklenmediyse abort
if [ -z "$(git diff --cached --name-only)" ]; then
  info "Hiç dosya stage edilmedi — boş commit önleniyor"
  git checkout -f "$DEFAULT_BRANCH"
  fail "Stage edilecek dosya yok: $AFFECTED_FILES"
fi

# ── Commit ──
COMMIT_MSG="self-improvement: $TITLE"
git commit -m "$COMMIT_MSG" || fail "commit başarısız"

# ── Push ──
git push -u origin "$BRANCH" --force-with-lease 2>/dev/null || {
  # İlk push'ta --force-with-lease çalışmayabilir (yeni branch)
  git push -u origin "$BRANCH" 2>&1 || fail "push başarısız"
}

# ── PR oluştur ──
PR_BODY=$(cat <<EOF
## Self-Improvement Önerisi

**ID:** $PENDING_ID
**Başlık:** $TITLE

Bu PR, self-improvement pipeline'ı tarafından otomatik oluşturulmuştur.
İnsan onayı ile başlatıldı.

**Etkilenen dosyalar:** $AFFECTED_FILES

---

_🤖 Otomatik oluşturuldu — lütfen değişiklikleri gözden geçirin._
EOF
)

PR_URL=$(gh pr create \
  --title "self-improvement: $TITLE" \
  --body "$PR_BODY" \
  --head "$BRANCH" 2>&1) || fail "PR oluşturma başarısız: $PR_URL"

info "PR oluşturuldu: $PR_URL"

# ── Pending kaydını güncelle (API çağrısı ile) ──
if [ -n "$INTERNAL_API_KEY" ]; then
  curl -s -X PATCH "$API_BASE/api/v1/self-improvement/pending/$PENDING_ID" \
    -H "X-API-Key: $INTERNAL_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"status\":\"pr_created\",\"pr_url\":\"$PR_URL\"}" >/dev/null 2>&1 || true
fi

echo "OUTCOME: pass | PR oluşturuldu: $PR_URL"