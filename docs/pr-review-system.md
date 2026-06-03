# Cross-Project Otomatik PR-Review Sistemi (klipper-orchestrated)

> **Amaç:** 7 ilgili repo'da açılan PR'ları otomatik kod-review'dan geçir; bulguları
> PR'a inline yaz. Codex'in güvenilmezliğini (atlama/latency) yapısal kapat:
> klipper tetikler, atlamaz. **Durum:** Faz 1 (poller + digest-aggregate) DONE+CANLI; Faz 2 (koşullu auto-review spawn) KURULDU/DISABLED (pilot claude-server) — surer cross-verify + ilk-5-spot-check sonrası ENABLED.

## Karar (kullanıcı, 2026-06-02)
- Tetikleme: **CI-yeşil sonrası**. Çıktı: **PR'a `--comment` (inline)**.
- Kapsam: **7 repo** (claude-server, panola, kuafor, petvet, bilge-arena, renderhane, koken-akademi).
- Rate-limit: `PR_REVIEW_MAX` (varsayılan 5/run) — her review = 1 Claude-spawn (kullanım maliyeti).

## Dürüst kısıt (kritik)
- `/code-review **ultra**` (bulut çok-ajan) **FATURALI + KULLANICI-TETİKLİ → programatik tetiklenemez** (ne klipper ne spawn). İnsan-tetikli kalır (derin-dalış / kritik PR).
- Sistem **lokal `/code-review high`** kullanır: in-session, Claude-kalitesi, faturasız (spawn-maliyeti hariç). PR#13'te lokal-high surer'ın cross-verify'ıyla AYNI residual'ı yakaladı — yeterli ilk-savunma, ultra kadar derin değil.

## Mimari
1. **`automation/pr-review-poller.sh`** (cron, klipper-cron-wrap'lı → FAZ1 outcome-contract): 7 repo'da açık PR tara → **CI-yeşil + draft-değil + bu-HEAD'de henüz review-edilmemiş** olanları ADAY seç. Idempotency: `data/hook-state/pr-review-state.json {repo#pr: reviewed_head_sha}` (HEAD değişirse yeniden-review). Rate-limit `PR_REVIEW_MAX`.
2. **Review-spawn (validated-next-step):** her aday için dedicated Claude spawn (surer-not-kanalından AYRI) → `cd <repo-local-checkout>` → `/code-review high <PR#> --comment`. Başarılı → `mark_reviewed`.
3. **FAZ1/2 entegrasyon:** poller `OUTCOME:` emit eder → `cron_outcomes` → liveness izler → digest "review-bekleyen/bulgulu PR" özeti.
4. **Codex-aggregate:** doğru API `gh api repos/O/R/pulls/N/comments` `.user.login=="chatgpt-codex-connector[bot]"` (inline) ile varsa Codex bulgularını topla. **Codex-sessiz ≠ temiz.**

## Güvenli rollout (auto-posting riski)
- **DRY_RUN=1 (varsayılan):** sadece adayları loglar. Şu an cron bu modda → günlerce candidate-detection'ı gözlemle.
- **ENABLED (DRY_RUN=0 + PR_REVIEW_ENABLED=1):** spawn+post açılır — AMA spawn entegrasyonu ayrı doğrulanmış adımda (tek-PR manuel spawn testi → sonra otomatik). 7 prod-repo'ya otomatik comment yazdığı için validation şart.

## Doğrulama (foundation)
- Poller dry-run: 7 repo, read-only, 0-aday (o an açık PR yok) DOĞRU.
- `ci_green` birim: all-SUCCESS→green; FAILURE/pending/boş→not-green (4/4).
- Idempotency: reviewed_head(repo#pr) == HEAD → atla.

## Açık / sırada
- Review-spawn entegrasyonu (autonomous-claude.sh not-bağlı; dedicated review-spawn gerekli) + tek-PR manuel doğrulama → sonra ENABLED.
- surer maliyet-sınırı/rol girdisi (VPS-repo'lar) bekleniyor.

## Faz 2 — kuruldu (DISABLED, pilot)
- `pr-review-spawn.sh`: headless `claude -p` direct-review + TEK özet bot-etiketli `gh pr comment` (multi-agent fan-out DEĞİL → Max-x20 bütçe-dostu). SPAWN_ENABLED guard.
- `pr-review-settings.json`: review-scope (Read + git-read + gh pr view/diff/comment; Write/commit/push/merge/sudo DENY).
- Poller FAZ2 trigger: main/master-hedef & (insan-flag `review-please` VEYA diff>400 VEYA Codex-sessiz). Caps: per-run 5 + günlük 10 (Max-x20 paylaşımlı koruma) + serialize + same-HEAD-skip. Pilot repo: claude-server.
- Billing = Max x20 (paylaşımlı). Codex-optim: trigger'da önce `@codex review` force (ücretsiz), sonra Claude-spawn fallback (sonraki iter).
- ENABLE: `DRY_RUN=0 + PR_REVIEW_ENABLED=1` (poller) + `SPAWN_ENABLED=1` (spawn). surer cross-verify + ilk-5 insan-spot-check ŞART.
