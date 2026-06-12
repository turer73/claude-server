#!/bin/bash
# pr-gate-lib.sh — PR merge-gate sinyallerini OKUYAN paylaşılan saf fonksiyonlar.
# Hem pr-review-poller.sh (otomasyon) hem scripts/check-pr-gate.sh (elle/pre-merge)
# BUNU source eder — tek-kaynak. Amaç: Codex/CI/Codecov durumunu HER YERDE aynı
# (doğru) mantıkla okumak. (Ders 2026-06-12: elle-poll yanlış login `chatgpt-codex-
# connector` (`[bot]`-suz) + sadece review-objesi bakıp Codex'in issue-comment
# review'ını kaçırdı -> "Codex gelmedi" yanlış sonucu. Bu lib o bug'ın kök-çözümü.)
#
# Side-effect YOK (sadece fonksiyon tanımı) — güvenle source edilebilir.

# CI-yeşil mi? statusCheckRollup JSON'unda FAIL/ERROR/pending yoksa + ≥1 SUCCESS varsa.
ci_green() {
  local rollup="$1"
  local bad ok pend
  bad=$(printf '%s' "$rollup" | jq '[.[] | select((.conclusion // .state // "") | test("FAIL|ERROR|CANCELLED|TIMED_OUT|ACTION_REQUIRED|STALE"; "i"))] | length' 2>/dev/null || echo 1)
  ok=$(printf '%s' "$rollup" | jq '[.[] | select((.conclusion // .state // "") | test("SUCCESS|NEUTRAL|SKIPPED"; "i"))] | length' 2>/dev/null || echo 0)
  pend=$(printf '%s' "$rollup" | jq '[.[] | select(((.status // "") | test("IN_PROGRESS|QUEUED|PENDING"; "i")) or ((.state // "") | test("PENDING|EXPECTED"; "i")))] | length' 2>/dev/null || echo 0)
  [ "${bad:-1}" -eq 0 ] && [ "${pend:-0}" -eq 0 ] && [ "${ok:-0}" -gt 0 ]
}

# Codex FORMAL-REVIEW durumu (current-HEAD): none | findings | clean | unknown
#   none     = bu-HEAD'de Codex review-entry YOK -> force-trigger gerek
#   findings = entry VAR + inline-comment VAR -> Codex bulgu buldu
#   clean    = entry VAR + inline YOK -> temiz onay
#   unknown  = gh-fail -> spurious karar yok
# Login DAİMA `chatgpt-codex-connector[bot]`. HEAD-filtre (commit_id==head): eski-commit
# review'ları stale sayılmasın. (Poller'ın orijinaliyle BİREBİR — davranış değişmez.
# Codex'in issue-comment-olarak gelen verdict'i için ayrı codex_issue_verdict'e bak.)
codex_state() {
  local repo="$1" num="$2" head="$3" rev inl reviews comments
  # NOT: `gh api --jq --arg` ÇALIŞMAZ (gh api --arg kabul etmez: "accepts 1 arg").
  # Bu yüzden `gh api` çıktısını jq'ya PIPE'la (--arg jq'ya geçsin). Eski poller
  # bu sözdizimini kullanıyordu -> codex_state hep "unknown" dönerdi; aday=0 olduğu
  # için prod'da hiç tetiklenmemiş latent bug'dı (2026-06-12 bu refactorda yakalandı).
  reviews=$(gh api "repos/$repo/pulls/$num/reviews" 2>/dev/null) || { echo unknown; return; }
  if [ -n "$head" ]; then
    rev=$(printf '%s' "$reviews" | jq --arg h "$head" '[.[]|select(.user.login=="chatgpt-codex-connector[bot]" and .commit_id==$h)]|length' 2>/dev/null || echo X)
  else
    rev=$(printf '%s' "$reviews" | jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length' 2>/dev/null || echo X)
  fi
  [[ "$rev" =~ ^[0-9]+$ ]] || { echo unknown; return; }
  [ "$rev" -eq 0 ] && { echo none; return; }
  comments=$(gh api "repos/$repo/pulls/$num/comments" 2>/dev/null || echo '[]')
  if [ -n "$head" ]; then
    inl=$(printf '%s' "$comments" | jq --arg h "$head" '[.[]|select(.user.login=="chatgpt-codex-connector[bot]" and (.commit_id==$h or .original_commit_id==$h))]|length' 2>/dev/null || echo 0)
  else
    inl=$(printf '%s' "$comments" | jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length' 2>/dev/null || echo 0)
  fi
  { [[ "$inl" =~ ^[0-9]+$ ]] && [ "$inl" -gt 0 ]; } && { echo findings; return; }
  echo clean
}

# Codex ISSUE-COMMENT verdict'i (review-objesi olmadan gelen "Codex Review: ..." gibi).
# #118 dersi: Codex bazen formal-review YERİNE issue-comment yazar -> codex_state "none"
# döner ama Codex aslında incelemiştir. En son codex issue-comment'inin tek-satır özetini
# echo'lar (yoksa boş). DİKKAT: issue-comment'te commit bağı YOK -> HEAD-tazeliği garanti
# edilemez; bu yüzden codex_state'in force-trigger kararını EZMEZ, sadece bilgi amaçlı
# (gate-helper yüzeye çıkarsın; insan/oturum HEAD-uyumunu teyit etsin).
codex_issue_verdict() {
  local repo="$1" num="$2" cmts v
  cmts=$(gh api "repos/$repo/issues/$num/comments" 2>/dev/null) || return 0
  # Gerçek verdict "Codex Review: ..." ile başlar; "To use Codex here, create an
  # environment..." gibi kurulum/gürültü mesajlarını ELE (en son verdict'i seç).
  v=$(printf '%s' "$cmts" | jq -r '[.[]|select(.user.login=="chatgpt-codex-connector[bot]" and (.body|test("Codex Review";"i")))]|last|.body // ""' 2>/dev/null)
  [ -z "$v" ] || [ "$v" = "null" ] && return 0
  printf '%s' "$v" | head -1 | cut -c1-140
}

# Codecov: en son codecov[bot] PR-yorumunu okuyup insan-okur durum-satırı echo'lar.
# Formatlar değişir: (a) minimal "All modified and coverable lines are covered" (patch
# %100, sorun yok), (b) "Patch coverage is `X%`" + project tablosu + "-Z%" düşüş.
# Çıkış: "" (yorum yok) | "covered" (a) | "patch=X% proje=Y% Δ=Z[ ⚠️DÜŞÜŞ]" (b).
codecov_patch() {
  local repo="$1" num="$2" body
  body=$(gh api "repos/$repo/issues/$num/comments" 2>/dev/null \
    | jq -r '[.[]|select(.user.login=="codecov[bot]")]|last|.body // ""' 2>/dev/null)
  [ -z "$body" ] || [ "$body" = "null" ] && { echo ""; return; }
  # (a) hepsi-kapsamda minimal yorum
  if printf '%s' "$body" | grep -qiE 'all modified and coverable lines are covered'; then
    echo "covered"; return
  fi
  # (b) yüzdeli rapor
  local patch proj delta out
  patch=$(printf '%s' "$body" | grep -oiE 'patch coverage is[^0-9]*[0-9.]+%' | grep -oE '[0-9.]+%' | head -1)
  proj=$(printf '%s' "$body" | grep -oiE 'project coverage is[^0-9]*[0-9.]+%' | grep -oE '[0-9.]+%' | head -1)
  delta=$(printf '%s' "$body" | grep -oE '[(][+-][0-9.]+%[)]' | head -1 | tr -d '()')
  out="patch=${patch:-?} proje=${proj:-?}"
  [ -n "$delta" ] && out="$out Δ=$delta"
  case "$delta" in -*) out="$out ⚠️DÜŞÜŞ";; esac
  echo "$out"
}
