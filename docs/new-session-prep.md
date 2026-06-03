# Yeni-Oturum Hazırlık (klipper) — 2026-06-03 sonrası

## Başlangıç durumu (temiz)
- `/opt/linux-ai-server` = **master** (servis-güvenli); `git worktree` mimarisi yerleşik (feature = `…-wt/<isim>`, /opt daima master). Bkz memory `reference-shared-worktree-collision-2026-06-03`.
- LIVESYS **FAZ 0-3 KOMPLE** (notify-cron CANLI, Telegram-direct, `*/20`).
- Güvenlik audit: **KRİTİK+ORTA canlı** (#27 RCE/privesc/fail-open, #28 path/perm, #25 creds).
- FAZ 4: **S1+S2 merged** (`scripts/blast-radius.sh`: tek-dosya + `--diff` changeset-mode).
- **Disiplinler:** HARD-RULE (merge = CI-green AND Codex-son-an-check), branch-ownership (her agent kendi branch+worktree), verify-against-code (Codex/surer iddiaları dahil).

## Hızlı ilk-işler (yeni oturum, kısa)
1. **Açık PR kontrol:** sürer #30 (batch-3 SSRF/install/env/DB) — cross-verify iste/bak.
2. **WAL #517** (klipper-auto shm/wal 644 → klipperos yazamaz). **NOT (bu oturumda çözülemedi):** klipper-auto standalone systemd-unit DEĞİL (note-poller/hook spawn üzerinden). UMask=0002'yi NEREYE koyacağı belirsiz → araştır: `klipper-note-poller.service` + hook-spawn zinciri (`scripts/hooks/`), klipper-auto'nun hangi process'ten doğduğunu izle; UMask o unit'e/spawn-wrapper'a. Geçici çözüm hâlâ: servis-restart.

## FAZ 4 — S3 SPEC (sıradaki, klipper)
**Amaç:** PR-review'a değişiklik-öncesi-etki enjekte et (FAZ4↔PR-review köprüsü; planın stated linkage).
**Ne:** `automation/pr-review-poller.sh` (ve/veya `pr-review-spawn.sh`) — aday-PR için:
1. PR'ın diff-range'inden `scripts/blast-radius.sh --diff <base>...<head>` çağır.
2. Çıktıyı review-prompt'a ek-blok olarak enjekte: "BLAST-RADIUS: bu PR şu tablolara dokunuyor [...]; consumer'lar [...]" → reviewer değişikliğin etki-alanını ÖNCEDEN görür.
**Dikkat:** PR-review FAZ2 DISABLED-pilot → S3 dormant-ama-hazır (enable'da devreye girer). poller'da `git fetch` + range hesabı (PR base/head SHA) gerekir. blast-radius read-only → güvenli.
**S4:** "yüksek-blast" (çok-tablo/çok-consumer) PR'da FAZ2 review-spawn-kararına ekstra-ağırlık sinyali.

## Açık iş tam listesi
- FAZ4 S3 (yukarı) → S4 · LIVESYS **FAZ 5** (kapalı-döngü müdahale = BÜYÜK, tasarım+kademeli-pilot+güvenlik-gate) · FAZ 6 (orkestra)
- Ops: WAL #517 · sürer batch-3 (#30) cross-verify
- notify-cron test artık `NOTIFY_ENV_FILE=/dev/null` ile izole (bu PR).

İlgili memory: `project-session-2026-06-03-security-faz4`, `project-faz32-producer-wiring-2026-06-03`, `feedback-merge-gate-codex-check-2026-06-03`.
