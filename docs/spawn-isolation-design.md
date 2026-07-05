# Spawn-Isolation Tasarım — otonom-spawn swarm/master-collision kök-fix

> **Lane:** tasarım = klipper · impl = surer. Amaç: otonom-spawn'ların paylaşılan `/opt` checkout'unu
> kirletmesini (edit + commit → local-master divergence) KÖKTEN önlemek.
> **Geçmiş:** PR#262 (cwd-only) KAPATILDI — Codex P1+3×P2: cwd-değişimi absolute-path-yazıları izole
> etmez (kök-kusur), cleanup commit'i yok eder, audit baypaslanır, retry-stale-worktree fallback.

## 0. Sorun (doğrulanmış, 2026-07-03 canlı)
Otonom-spawn (`autonomous-claude.sh` + `autonomous-spawn-retry.sh`) `/opt/linux-ai-server`'da (master'da)
koşuyor. Guardrails spawn'a `/opt/linux-ai-server/**` edit + `git commit (push YOK)` diyor. Sonuç:
1. **Edit-pollution:** spawn ana-checkout'un working-tree'sini kirletir (absolute-path edit).
2. **Commit-divergence:** spawn `git commit` → local-master origin'den ayrılır (f51840b/aa59d85/34d01d62 —
   bugün 3+ collision, her biri manuel-reconcile gerektirdi).

## 1. Neden cwd-only YETMEZ (Codex PR#262 dersleri — tasarıma dahil)
- **[P2-a KÖK] Yazı-yönlendirme yok:** cwd=worktree yapmak, spawn'ın `/opt/linux-ai-server/app/foo.py`
  (absolute) yazmasını engellemez → ana-checkout yine edit'lenir, üstelik worktree-git bu edit'i GÖRMEZ.
- **[P1] Commit-koruma yok:** guardrails `commit (push YOK)` → commit yalnız detached-worktree'de →
  `worktree remove` onu YOK EDER → work-loss (not "başarılı" ama kod kayıp).
- **[P2-b] Audit baypası:** `autonomous-spawn-audit.sh` spawn-head'i `/opt rev-parse HEAD`'le kıyaslar →
  izole-commit HEAD ilerletmez → "unchanged" → sensitive-file/secret denetimi ATLANIR.
- **[P2-c] Retry stale-worktree:** sabit-path `retry-spawn-<id>`; kill-sonrası kalır, `prune` silmez →
  sonraki `worktree add` fail → shared-checkout fallback → collision geri gelir.

## 2. Doğru mimari
### 2.1 Yazı-izolasyonu = ENFORCED (P2-a çözümü, çekirdek)
cwd yetmez → **spawn'ın Edit/Write'ını worktree-DIŞINA DENY et** (deterministik, prompt-umuduna değil):
- Per-spawn **settings** üret: `PreToolUse` deny-hook `Edit|Write` path'i worktree-subtree DIŞINDAysa
  reddeder (`/opt/linux-ai-server/**` ama worktree-değil → DENY; `/home/klipperos/work/**` opsiyonel-izin).
- Prompt'a: "repo bu spawn için `<worktree>`'de checkout'lu; TÜM dosya-işlerini orada yap; ana-checkout'a
  yazma." + note-içeriğindeki `/opt/linux-ai-server/...` absolute-path'leri worktree-relative'e çevir-uyarısı.
- Sonuç: spawn ana-checkout'a YAZAMAZ (hook-enforce); tüm iş worktree'de.

### 2.2 Commit-koruma (P1 çözümü)
Cleanup ÖNCESİ: worktree-HEAD base'den ilerlemişse (spawn commit-yaptı) → **durable ref'e kaydet**
`refs/spawn-work/<note_id>-<nonce>` (+ opsiyonel push `spawn/<note_id>` branch). SONRA worktree-remove.
Commit'ler korunur + insan/audit erişebilir. (Alternatif: guardrails-step5 → "commit + push spawn-branch";
ama auto-ref-preserve daha güvenli — spawn-push'a bağımlı değil.)

### 2.3 Audit worktree-ref'e bakar (P2-b çözümü)
`autonomous-spawn-audit.sh`'e spawn-ref'i (2.2) GEÇİR; audit `/opt HEAD` yerine o ref'in diff'ini
tarar (sensitive-file/secret). İzole-commit'ler artık denetlenir.

### 2.4 Nonce-path + stale-repair (P2-c çözümü)
Worktree-path `spawn-<note_id>-<epoch>-<pid>` (nonce → çakışma-yok). Pre-run: bu-note için stale
worktree'leri `worktree remove --force` + `prune`. Fallback-YERINE-repair: `worktree add` fail →
stale-temizle-tekrar-dene; yine fail → **fail-CLOSED değil**: shared-fallback AMA CRITICAL-emit + audit-flag
(pollution-riski görünür; sessiz-fallback yok).

### 2.5 Fail-safe
Worktree-altyapısı çökerse spawn-DURMASINDANSA shared-fallback (spawn işlevini korur) AMA
warn/critical-emit (pollution-riski operatöre görünür). Gate-mekaniği spawn'ı asla kilitlemez.

## 3. Kabul kriterleri (surer impl)
1. Spawn ana-checkout'a Edit/Write YAPAMAZ (deny-hook testli: worktree-dışı-path → reddedilir).
2. Spawn-commit `worktree remove` sonrası KORUNUR (ref/branch'te; work-loss-yok testli).
3. `autonomous-spawn-audit.sh` izole-spawn-commit'i denetler (sensitive-file testli).
4. Nonce-path; kesik-retry sonrası stale-repair (fallback-değil) testli.
5. Ana-master spawn-sonrası DEĞİŞMEZ (izolasyon E2E; bugünkü collision-senaryosu regresyon-testi).
6. Fail-open: worktree-altyapı-hatası → shared-fallback + critical-emit (sessiz-değil).
7. bash -n + shellcheck temiz.

## 4. Fazlama
- **Faz-1** (P1+P2-b+P2-c, düşük-risk): nonce-path + commit-ref-koruma + audit-ref. Yazı hâlâ shared
  ama commit-izole+korunur+denetlenir. Divergence'ı azaltır.
- **Faz-2** (P2-a, çekirdek, dikkatli): yazı-deny-hook + prompt-yönlendirme. Tam-izolasyon.
- Her faz ayrı-PR + Codex-review (safety-critical → Opus x2) + E2E-collision-regresyon-testi.

## 5. Alternatif (basit-reaktif, eğer 2.1 çok-riskli görülürse)
Post-spawn: ana-master `pre_spawn_sha`'dan ilerlemişse → commit'i `spawn/<note>` branch'ine taşı +
`git reset --keep pre_spawn_sha`. Klipper'ın bugün manuel-yaptığı. Düşük-risk, kısmi (edit-pollution
kalır ama commit-divergence temizlenir). Faz-2 riskliyse köprü-çözüm.

---

## Güvence-kapsamı — DÜRÜST ETİKET (Fable 3.-perspektif, 2026-07-05)

**Faz-2 write-guard bir GÜVENLİK-SINIRI DEĞİL — KAZA-sertleştirmesidir.**

- **Kapatır:** kafası-karışık-ama-dürüst spawn'ın Edit/Write-tool'uyla `/opt` ana-checkout'a yazma kazasını (deterministik DENY).
- **KAPATMAZ:** adversarial / prompt-injected spawn. Sebep: spawn base-settings'i `Bash(echo *)`, `Bash(cat *)`, `Bash(find *)`, `Bash(jq *)` gibi redirect-yetenekli komutlara izin verir; Claude Code Bash-permission'ı komut-prefix'ine bakar, **redirection'ı (`> /opt/...`) GÖRMEZ** → guard (yalnız Edit|Write|MultiEdit|NotebookEdit matcher'ı) hiç çalışmaz. `echo x > /opt/linux-ai-server/app/main.py` guard'ı bypass eder.
- **Kalan bilinen-P2'ler (kabul, kovalanmıyor — edge-case-numaralandırması):** shared-fallback-audit-base (retry:222), /tmp-backed-pool-settings (lib:108 — SPAWN_WT_BASE'i /tmp altına KOYMA), uncommitted-retry-work-preserve (retry:304).

**Tehdit-modeli bugün:** spawn'lar güvenilir-input'la kaza-yapabilir (adversarial-değil) → Faz-1(worktree-izolasyon) + Faz-2(kaza-guard) YETERLİ.

**"Spawn'lar adversarial" günü (gelecek) gerçek-cevap: UID-izolasyon** — spawn'ı ayrı-uid'le koş + worktree'yi ona chown (veya bwrap/namespace). Kernel HER kanalı (Edit, Bash, python, git, redirect) sıfır-per-caller-entegrasyonla enforce eder → app-layer-guard'ın tüm-bulgu-sınıfı (4-tur/17-Codex-bulgu) buharlaşır. 4-tur-kaskadın dersi: **mekanizma-yanlış-değil, KAT-pahalı** (app-layer'da entegrasyon-tamamlığı tanım-gereği hiç-bitmez). uid = doğru-kat, adversarial-gün için.
