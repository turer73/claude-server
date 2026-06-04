# Yeni-Oturum Hazırlık (klipper) — 2026-06-04 sonrası

## Başlangıç durumu (temiz)
- `/opt/linux-ai-server` = **master** (servis-güvenli); `git worktree` mimarisi yerleşik (feature = worktree, /opt daima master). Bkz memory `reference-shared-worktree-collision-2026-06-03`.
- LIVESYS **FAZ 0-3 KOMPLE** + **FAZ 4 KOMPLE** (S1-S4 blast-radius; S3 review-prompt enjeksiyonu + S4 yüksek-blast spawn-sinyali).
- **batch4 5/5 KAPANDI** + **2 canlı secret-açığı kapatıldı** (JWT public-default + Telegram public-leak); kök-neden #5 ile giderildi (secret'lar env-only). Bkz `## Güvenlik (kapandı)` ve memory `project-session-2026-06-03-takeover-batch4-faz4s3`.
- **Disiplinler:** HARD-RULE (merge = CI-green AND Codex-son-an-check; Codex re-review sık force-trigger gerektirir, gelmeyince fallback=bağımsız-kod-verify), branch-ownership (worktree), verify-against-code (Codex iddiaları dahil — #36'da commit-hash halüsinasyonu yakalandı).

## Sıradaki iş (öncelik sırası)
1. **FAZ 2 PR-review ENABLE** (DISABLED-pilot → canlı): S3+S4 hazır besliyor. ENABLE-checklist: cross-verify → spot-check → cron. Ücret: Max-x20 paylaşımlı, cap'li. Detay `docs/pr-review-system.md`.
2. **LIVESYS FAZ 5** (kapalı-döngü müdahale = BÜYÜK): atıl-sınıflandırma + dormant-remediation canlandır + doğrulama + rollback/eskale + güvenlik-gate. Tasarım + kademeli-pilot.
3. **FAZ 6** (orkestra rolü/sınır). · Opsiyonel **FAZ4-S5=AST** (blast-radius heuristic→kesin).

## Açık ops/borç
- **#517 WAL** (kısmen): cron-wrap busy_timeout eklendi (PR#32, BUSY-kaybı çözüldü); klipper-auto'nun shm/wal'ı 644 yaratma kök-nedeni (READONLY senaryosu) hâlâ açık olabilir — note-poller(`klipper-auto` user) + hook-spawn zinciri UMask incelenmeli.
- **VPS → surer:** backup.sh #513 + panola-social webhook deployed kopya (eski-ölü Telegram token, fonksiyonel) — #99817.
- **Autonomous-spawn threat-scanner FP'leri** (aşağı TELEGRAM-STREAM bölümü) — whitelist tuning.
- Plan-doc kozmetik: repo `config/server.yml` NESTED vs deployed FLAT drift (secret artık yaml'dan yüklenmiyor → zararsız).

İlgili memory: `project-session-2026-06-03-takeover-batch4-faz4s3`, `security-jwt-secret-public-default-2026-06-03`, `security-telegram-token-leak-2026-06-04`, `feedback-merge-gate-codex-check-2026-06-03`.

## Güvenlik (KAPANDI — batch4 #99804 + secret-leak'ler, 2026-06-03/04)
- **#1** jwt_secret env-only + create_app placeholder-guard → **PR#33** (prod public-default `change-me-via-env` rotate edildi).
- **#2** shell dürüst-doc + regex katastrofik-blok + audit-log → **PR#34** (tam-shell korundu, kullanıcı kararı).
- **#3** allowed-path scope → **NON-ISSUE** doğrulandı (MCP'de delete yok; files-API delete zaten allowed_paths-scope'lu; geniş MCP file_write bilinçli korundu).
- **#4** CI [3.11,3.14] matrix → **PR#38** (her iki sürüm yeşil).
- **#5** config-drift kök-çözüm → **PR#37** (TÜM secret'lar `_SECRET_FIELDS` YAML'dan dışlanır = env-only + nested-recursive drift-WARNING; supabase+coolify drop-in'e taşındı, server.yml secret=0).
- **#6** kırık-test-üstünde-servis: ilgili #512/#515/#518 stale-FP kapatıldı (master yeşil).
- **#7** notify-cron ENABLED+no-pending OUTCOME testi → surer (bilge-arena dışı, tamamlandı).
- **Telegram bot-token PUBLIC-LEAK** (GitHub secret-scan, webhook_server.py) → **PR#36** + kullanıcı revoke + klipper(.env/drop-in/server.yml) + n8n-4wf-DB-replace rotate.
- **DERS:** secret world-readable YAML'dan gelemez (server.yml 0644) + Settings(**filtered) env'i ezer → bu kombinasyon iki leak'in de köküydü, #5 ile kapandı.

## TELEGRAM-STREAM triyaj (2026-06-03, kullanıcı ileti)
Çoğu info/sağlıklı. Actionable:
- **VPS-backup vol 5/6** (1 volume eksik, tekrarlayan) — backup-bütünlük, triyaj-kuyruğu (memory `761`). pull-vps-backup partial-rc=0 doğru raporluyor (FAZ1 çalışıyor).
- **E2E residual:** Panola 120/121 (1-fail) + kuafor-sidebar-desktop-fail — bilinen, ayrı-panola işi.
- **🟢 Autonomous-spawn threat-scanner FALSE-POSITIVE'leri** (#99776 cred-env-cat-own-.env, #99797/#99803 exfil-curl-pipe = not-okundu-işaretleme curl'ü): ajanların MEŞRU iç-operasyonları flag'leniyor (high-recall). **Scanner-whitelist tuning:** (a) spawn'ın kendi /home/klipper-auto/.env bootstrap-okuması, (b) localhost `/api/v1/memory/notes/*/read` curl'ü → whitelist. Yoksa her not-read "tehdit" alarmı = alarm-yorgunluğu. [klipper, küçük-orta, yeni-oturum]
