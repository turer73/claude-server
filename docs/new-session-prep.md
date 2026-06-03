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

## BATCH-4 — kullanıcı bulguları (2026-06-03, surer #99804 ileti) — YENİ-OTURUM triyaj
7 bulgu değerlendirildi (wind-down'da ertelendi, yeni-oturum):
1. **jwt_secret config-içinde** → env-only yapılmalı (`app/core/config.py`). [klipper, küçük-orta]
2. **Shell whitelist geniş** (rm/reboot/mkfs `config.py:86`) → "whitelist" yanıltıcı; gerçek-whitelist VEYA dürüst-adlandır + tehlikeli-komut-blok. [klipper, orta] (FAZ4-S3 DEĞİL — ayrı güvenlik.)
3. **Allowed paths geniş** (/proc,/sys,/etc file_manager) → yetki-seviyesi ayrımı (read vs admin path-scope). [klipper, orta]
4. **CI 3.11-only, prod 3.14** → CI matrix [3.11, 3.14] (özellikle tarfile filter / 3.12+ özellikleri). [klipper, küçük — `.github/workflows/ci.yml`]
5. **Repo↔canlı config drift** (schema-validation yok) → startup config-schema-validate (.env/systemd/yaml tutarlılık). [klipper/surer ortak, orta] (#11 env-path + .env-dual-key ailesi.)
6. **Kırık-test üstünde servis** (#515/#518) → zaten loglu, triyaj-kuyruğu.
7. **notify-cron test** → surer: disabled-case test var, OUTCOME enabled-yolda; **gerçek boşluk: ENABLED+no-pending OUTCOME:pass testi yok** → ekle. [surer/klipper, küçük]

**Domain-split:** klipper=#1/#2/#3/#4 (app-config+CI), ortak=#5, surer=#7 (notify-cron). Öncelik: #4(CI-matrix kolay+değerli) + #1(jwt-secret kolay) → sonra #2/#3 (auth-hardening) → #5 (drift). HARD-RULE + worktree + branch-ownership uygula.

## TELEGRAM-STREAM triyaj (2026-06-03, kullanıcı ileti)
Çoğu info/sağlıklı. Actionable:
- **VPS-backup vol 5/6** (1 volume eksik, tekrarlayan) — backup-bütünlük, triyaj-kuyruğu (memory `761`). pull-vps-backup partial-rc=0 doğru raporluyor (FAZ1 çalışıyor).
- **E2E residual:** Panola 120/121 (1-fail) + kuafor-sidebar-desktop-fail — bilinen, ayrı-panola işi.
- **🟢 Autonomous-spawn threat-scanner FALSE-POSITIVE'leri** (#99776 cred-env-cat-own-.env, #99797/#99803 exfil-curl-pipe = not-okundu-işaretleme curl'ü): ajanların MEŞRU iç-operasyonları flag'leniyor (high-recall). **Scanner-whitelist tuning:** (a) spawn'ın kendi /home/klipper-auto/.env bootstrap-okuması, (b) localhost `/api/v1/memory/notes/*/read` curl'ü → whitelist. Yoksa her not-read "tehdit" alarmı = alarm-yorgunluğu. [klipper, küçük-orta, yeni-oturum]
