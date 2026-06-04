# FAZ 6 — Orkestra Rolü / Sınır (LIVESYS Yetenek 5)

> **İlke (build DEĞİL, sınır):** Kalıcı/tekrarlayan iş = **deterministik kod** (cron/script/SQL).
> Claude (LLM) = **büyük/belirsiz/yargı** kararlarının şefi; **olay-omurgasıyla tetiklenir,
> kalp-atışı (timer-ile-sürekli-düşünme) DEĞİL.** Bu faz feature eklemez — sınırı tanımlar,
> mevcut sistemi denetler, gelecek eklemeler için kural koyar.

## 1. Karar kuralı (her yeni otomasyon eklerken sor)
1. **Tekrarlayan + deterministik mi?** (eşik-kontrol, retry, agregasyon, liveness) → **KOD** (cron/script). LLM ÇAĞIRMA.
2. **Büyük/belirsiz/yargı mı?** (not-triyaj, PR-review, plan, remediation-kararı) → **Claude**, ama:
   - **Olay-tetikli** olmalı (not-gelişi, PR-aday, alert, onay, fail-kuyruğu) — timer'da "boşa düşünme" YOK.
   - Olay yoksa Claude koşmaz (kalp-atışı değil).
3. **GitHub gibi push-edemeyen kaynak?** → **poll-to-derive-event** meşru (poll deterministik; LLM yalnız gerçek-aday'da spawn). Tek istisna sınıfı.

## 2. Mevcut LLM-çağırma yüzeyi — denetim (2026-06-04)

### A. Ağır LLM (`claude -p` headless spawn) — hepsi EVENT-tetikli ✓
| Çağrı | Tetikleyici | Tip | Uyum |
|-------|-------------|-----|------|
| `autonomous-claude.sh` | note-poller (yeni-not) + `AUTONOMOUS_MODE` gate | olay (not-gelişi) | ✓ |
| `pr-review-spawn.sh` | pr-review-poller (CI-yeşil + flag/diff/blast aday) | poll→olay (aday-gated) | ✓ (istisna §1.3) |
| `execute-approved-plan.sh` | kullanıcı-onayı (note) | olay (onay) | ✓ |
| `autonomous-spawn-retry.sh` | cron */15 — `spawn_failures` DLQ | olay-türevi kuyruk (deterministik retry) | ✓ |

### B. Yerel LLM (Ollama) — on-demand / deterministik
| Çağrı | Tip | Uyum |
|-------|-----|------|
| `app/api/{ai,llm,rag,research,classifier,logs}` | on-demand (API isteği) | ✓ (kalp-atışı değil) |
| `autonomous-classifier-v2.sh` (qwen) | olay (not triyajı) | ✓ |
| `autonomous-health-check.sh` Ollama-probe (5-token) | deterministik **liveness-probe** (yargı değil) | ✓ |

### C. Scheduled cron — DETERMİNİSTİK (LLM-yargı YOK) ✓
| İş | Ne yapar | Uyum |
|----|----------|------|
| `autonomous-daily-summary` (06:00) | log/db agregasyon (cost/spawn özeti) — **LLM çağırmaz** | ✓ deterministik |
| `autonomous-health-check` (4h) | Ollama/DB liveness + orphan-lock temizliği | ✓ deterministik |
| `pr-review-poll` (2h) | PR-aday tarama → spawn aday'da | ✓ poll-to-derive-event |
| `digest-send`, `notify-cron`, `*-monitor`, vb. | deterministik kayıt/bildirim | ✓ |

## 3. Denetim sonucu
**Sistem ilkeye UYUYOR.** Ağır-LLM yalnız olay-tetikli; scheduled-cron'lar deterministik (agregasyon/liveness/retry) veya push-edemeyen-kaynak için poll-to-derive-event. **Kalp-atışı-LLM-yargısı (timer'da boşa-Claude-koşturma) YOK.** İhlal bulunmadı.

**Tek meşru istisna:** `pr-review-poll` timer'da koşar — çünkü GitHub PR-olayı klipper'a push edemez; poll deterministiktir, LLM yalnız gerçek-aday'da spawn olur (cap 5/run+10/gün).

## 4. Rol sınırı (klipper ↔ Claude ↔ surer)
- **Deterministik altyapı** (cron-wrap/outcome-contract/events/blast-radius/remediation-gate) = kod, kendini doğrular (FAZ1-5). Claude'a sormaz.
- **Claude** = yalnız yargı-anı şefi (olay gelince): triyaj, review, plan, remediation-onayı. Aksiyon sonrası **deterministik verify** (FAZ5-S2) — Claude'a "oldu mu" diye sorulmaz.
- **klipper↔surer** = iş-bölümü + karar koordine; otomasyon-mesajı `notes` (agent-to-agent), kullanıcı-hitabı yok.

## 5. Gelecek-kural (regresyon önleme)
Yeni LLM-çağrısı eklemeden önce §1 kuralını uygula. **Yasak:** LLM'i sabit timer'da yargı-işi için koşturmak (kalp-atışı). **Zorunlu:** olay-tetik veya gerekçeli-poll + cap.

---
*FAZ 6 KOMPLE: sınır tanımlı + mevcut-sistem denetlendi (uyumlu) + gelecek-kural konuldu. "build değil, sınır" — kod eklenmedi.*
