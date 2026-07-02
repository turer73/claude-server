# GAP-1 Kapsam-2: Cross-Agent Dispatch + Spawn-Authority Denetimi (Tasarım)

> **Lane:** design=klipper, impl=surer. **Durum:** taslak, kullanıcı-onayı bekliyor.
> **Bağlam:** Kapsam-1 (ci_fixer diff, PR#247 CANLI d7895d3) çıktı-tarafı deterministik denetimi
> kurdu. Kapsam-2 aynı prensibi **cross-agent dispatch** yüzeyine taşır — bug #1222
> (otonom-spawn dispatch-authority) + #100248 over-reach'in doğrudan kapatması.

## 1. Kapsam (2 yüzey + 1 önkoşul)

| Yüzey | Kod | Değer | Neden |
|---|---|---|---|
| A. Cross-agent dispatch notu | `create_note` (memory/notes.py:35) ← `_send_to_surer` (dispatch.py:230) + autonomous-spawn | **YÜKSEK** | #1222/#100248; tek-interception noktası; hiç denetlenmiyor |
| B. dispatch shell (KLIPPER) | `_run_klipper_cmd` (dispatch.py:197) | DÜŞÜK | ShellExecutor ZATEN input-gated (Codex-hardened: chain/interpreter/find-exec/whitelist) |
| (Kapsam-1) ci_fixer diff | — | — | PR#247 CANLI |

## 2. ⭐ Surface A-1: Task-package İÇERİK taraması (origin-agnostik)

**Interception:** `create_note` — TÜM not-yazımları buradan geçer (tek choke-point).
**Tetik:** `to_device` dolu (cross-agent) VE content bir task-paketi (JSON, `adimlar[]`/`cmd`/`command`/`komut` alanı var).
**Tara:** `scan_dispatch_note(content)` — `action_review._load_destructive_patterns()` REUSE + **ALAN-FARKINDA bağlamsal-whitelist:**
- YALNIZ executable-alanları tara: `adimlar[]`, `cmd`, `command`, `komut`, `steps[]`.
- `title`/`aciklama`/`description`/`content`-prose/`attack_type` gibi meta/prose alanları TARAMA.
- **Gerekçe (5×-FP dersi, [[feedback_pattern_match_contains_vs_mentions]]):** klipper'ın KENDİ dispatch-notları (#100261/#100269/#100271) yıkıcı-desenleri *analiz-prozası* olarak içerir ("chmod-x-guard", "recursive-force-delete"). Alan-farkında olmazsa HER meşru-dispatch-notu FP verir → tüm koordinasyon-kanalını bozar.

**Sinyal:** executable-alanda yıkıcı-desen → `emit_event(action-review, warn, source=dispatch)`. **notify-only.** Not YİNE yazılır (koordinasyon-kanalı fail-open).

## 3. ⭐ Surface A-2: Otonom dispatch-authority (#1222 / #100248)

**Problem:** Otonom-spawn `from_device='klipper'` yazıyor (interactive'den AYIRT EDİLEMEZ) → #100248'de otonom-ajan design+task dispatch etti, human-gate atladı, hiçbir katman yakalamadı.

**Önkoşul (instrument):** otonom-origin etiketleme. `autonomous-claude.sh` not-yazımları `from_device='klipper-autonomous'` kullansın (memories'de ZATEN `source_device='klipper-autonomous'` var — notlara genişlet). Alternatif: `X-Autonomous-Origin` header → `create_note` okur.

**Tespit:** `create_note`, ORIGIN=autonomous + cross-agent + CONSEQUENTIAL (task-paketi-şekli / design-dispatch) notu → `emit_event(action-review, warn, "otonom cross-agent dispatch — human-gate?")`. **notify-only.** #100248 tam bunu yakalardı.

**Faz2 (gate, opsiyonel):** otonom consequential-dispatch'i DENY/HOLD → human-onay iste (dispatch-authority kısıtı). notify-only'den ölçümle geçilir.

## 4. Surface B: dispatch shell (minimal)
ShellExecutor zaten input-gated. action_review EK-gate getirmez. Opsiyonel: dispatch edilen komutu events'e AUDIT-log'la (izlenebilirlik), yeni-gate yok. Düşük öncelik.

## 5. notify-only + fail-safe + ölçüm
- **notify-only Faz1.** Not-yazımı **fail-OPEN**: scan çökerse not YİNE yazılır (koordinasyon-kanalı kritik) + `emit_event(warn, "dispatch-scan taranamadı")`.
- **GAP-2 harness genişlet:** eval-set'e dispatch-notu vakaları — benign-dispatch (meşru task-paketi + prose-mentions-benign) vs malicious (adimlar[]'da yıkıcı-op) + autonomous-origin-consequential. catch/false-block ölç → Faz2-gate.
- **Bağlamsal-whitelist regresyon ZORUNLU:** "adimlar[]-contains vs prose-mentions" — klipper'ın kendi dispatch-notlarının benign kaldığını test et.

## 6. Entegrasyon + reuse
- `app/core/action_review.py` REUSE: `_load_destructive_patterns` + contextual-scan → yeni `scan_dispatch_note(content, from_device, to_device)`.
- Interception: `app/api/memory/notes.py::create_note` (fail-open + notify-only emit).
- Önkoşul: `automation/autonomous-claude.sh` not-yazımına origin-tag (from_device='klipper-autonomous').
- İlişki: **#1222'yi kapatır**, #100248 motive-vaka, Kapsam-1 makinesini yeniden-kullanır, [[project_gap1_action_review_design_2026_07_02]].

## 7. Kapsam-dışı (anti-hype)
- Otonom-dispatch BLOKLAMA (Faz2 policy). LLM-intent-review (deterministik-only, GAP-2 kanıtı). dispatch-shell re-gating (zaten kapsanmış). Adversarial-evasion-variantları (Faz2 #1224 ile aynı sınıf).
