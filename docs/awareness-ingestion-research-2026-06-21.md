# Farkındalık-Altyapısı: Ingestion/Detection Araştırma Sentezi — 2026-06-21

> **Bağlam:** klipper "tüm uyarı/hatalardan haberdar olma" hedefiyle **LSA (Yaşayan Sistem
> Farkındalığı) = aggregation + delivery** katmanını kuruyor (`docs/living-system-plan.md`,
> agent-feed.sh + heartbeat + LLM-sentez). Bu doküman **ingestion/detection** tarafını
> (surer-lane) kapsar: hangi yeni **sinyal-üreticileri** mevcut `events`-spine'a beslenmeli.
>
> **Lane-split:** surer = implement (ingestion producer'lar) · klipper = final-design + Linux-verify
> (LSA aggregation/delivery) · opencode = advisory/tasarım.
>
> **Kaynak:** GitHub deep-research (6 açı, 18 kaynak, 90 iddia). ⚠️ **Dürüstlük:** doğrulama-fazı
> Anthropic-rate-limit yedi (25 iddia "abstain" = *çürütülmedi, doğrulanamadı*). Aşağıda
> **✅ = bilgi-doğrulanmış** (gerçek, bilinen repo/pattern), **🔶 = adopt-öncesi-canlı-verify**.

---

## 0. KRİTİK grounding — gap(1) "event-spine" ZATEN VAR

origin/master taraması: `server.db.events` tablosu + `app/core/events.py::emit_event()` +
`scripts/emit-event.sh` + adapter'lar (cron_outcomes/liveness/alerts/pr/deploy/fix/backup/
health-check) → digest+alert okur → notify-cron → Telegram. Bu **LIVESYS Faz 3.2** = aranan
birleşik-omurga. **Karar (klipper #100122): mevcut `events` governs, yeni-tablo YOK**; ek-semantik
(fingerprint/novel/status) **payload-JSON**'a; gerçek-kolon ancak dedup-at-write kanıtlanınca.

➡️ **Sonuç:** "spine kur" değil — **mevcut spine'a EKSİK PRODUCER'lar ekle.** Aşağısı bu producer'lar.

---

## 1. Sinyal kaynakları (producer'lar) — value/effort sıralı

### gap-7 · Agent self-introspection (watchdog) — ✅ YAPILDI (PR#183)
Klipper'ın 88°C-incident'i (4 kaçak scanner %100×4core 17-25dk, `agent_freshness()` görmedi).
- **Ne:** psutil runaway-process (core-pinned + süre) + heartbeat-stall (`data/hook-state/*.json`) → `emit_event(agent-health)`.
- **FP-önleme** (klipper #100115): cömert-eşik >%90+>15dk, zorunlu allowlist (pytest/ruff/ollama→asla-kill), kademeli-kill, `/proc/cmdline`-verify, AUTO_KILL default-OFF.
- **Port:** `app/core/agent_watchdog.py` + cron (cron-wrap timeout+lock klipper-side). **Durum: PR#183, Linux-verify bekliyor.**
- **Follow-up:** producer-side dedup (per-run re-emit flood) · daemon heartbeat-stall (devops `metrics_history` yazım-tazeliği proxy, >5dk=stall).

### gap-2 · Application exception-tracking — ✅ baz hazır, düşük efor
Klipper SİSTEM'i izliyor ama **kendi FastAPI exception'larını/traceback'lerini** events'e yazmıyor = en büyük "tüm hatalar" boşluğu.
- **Pattern:** library-level capture. **✅ `sentry-sdk`** → self-hosted **✅ GlitchTip** (ama Django+PG, ağır) VEYA **daha hafif:** FastAPI exception-middleware → fingerprint (exc-tipi + top-frame) → `emit_event(type=exception, severity=error)`. 🔶 `Bugsink` (tek-binary, hafif) — verify.
- **Port:** `app/middleware/` exception-middleware → fingerprint → `emit_event`. "novel-vs-known" = fingerprint events'te ilk-kez mi.
- **Effort:** düşük (mevcut middleware-stack + emit_event).

### gap-3 · Log → structured novelty (Drain3) — ✅ yüksek-değer, düşük-orta efor
- **Pattern:** **✅ `logpai/Drain3`** — online log-template-miner (akıştan kümeleme, saf-Python, CPU). `change_type` = yeni/değişen küme → **NOVEL hata** → `emit_event(type=log-novelty, warn)`.
- **Port:** cron/stream `journalctl -u linux-ai-server` → Drain3 (state SQLite/dosya) → novel-template → emit_event. TR-tuzak yok (template-mining dil-agnostik).
- **Effort:** düşük-orta (pip Drain3 + cron).

### gap-4 · Dynamic/ML anomali (static-eşik ötesi) — orta-değer, orta efor
- **Pattern:** **✅ `river`** (online-ML, pip, CPU) VEYA Seasonal-ESD (S-H-ESD: trend+mevsim ayrıştır, residual'da ESD-test). 🔶 `LightESD`/`dtaianomaly` (2025 arXiv — verify; ama S-ESD pattern'i köklü, Twitter AnomalyDetection).
- **Port:** cron metrik-oku (`metrics_history`) → river/S-ESD → "bu-saat-için-anormal" → `emit_event(type=anomaly, warn)`. devops static-eşiğini **tamamlar** (eşik-aşmadan yakalar).
- **Effort:** orta (online-model state + cadence).

### gap-8 · Deployed≠running / intent≠live drift — orta-değer, düşük efor
- **Mevcut:** `main.py::_DEPLOYED_SHA` vs `_current_disk_sha` + dead-gate (config-effect). Genelleştir.
- **Port:** cron drift-check (running-SHA vs deployed-SHA, .env-effect, DB-schema vs migration) → `emit_event(type=drift, warn)`. dead-gate guard'ın runtime-genişlemesi.
- **Effort:** düşük (mevcut SHA-helper + dead_gate.audit).

---

## 2. Aggregation/delivery (klipper-lane — referans, surer-implement DEĞİL)

- gap-5 **Korelasyon/dedup/fatigue:** **✅ `keephq/keep`** pattern'leri (fingerprint-dedup + 15dk zaman-pencereli incident-grupla). *Platformu değil pattern'i.* Cross-source korelasyon events üstünde.
- gap-6 **LLM-consumable observability:** **✅ OTel + observability-MCP** (`traceloop/opentelemetry-mcp-server`) + **✅ ReAct-RCA** (arXiv 2403.04123: tool-donanımlı ReAct, static/RAG-RCA'yı factual-accuracy'de geçer). klipper'ın MCP'sine "şu an ne bozuk" + drill-down tool'ları (events-query/journalctl/systemctl) → reasoning-anında canlı-sorgu.
- **AIOps-taxonomy** (arXiv 2406.11213): preprocess→perceive→RCA→remediate; klipper perceive(alerts)+remediate(devops) güçlü, eksik = unified-preprocess(events✓) + agent-RCA-tools.

> ⚠️ **Deploy-ETME (ağır/k8s):** SigNoz (ClickHouse), full-Keep, GlitchTip(Django+PG) — pattern-referansı, klipper'ın SQLite+events'i yeterli.

---

## 3. Önerilen sıra (surer-ingestion)

1. **gap-7 watchdog** — PR#183 ✅ (88°C-defense, en yüksek öncelik) → verify+merge.
2. **gap-2 app-exception** (düşük efor, en büyük "tüm-hatalar" boşluğu) → middleware→emit_event.
3. **gap-3 log-novelty (Drain3)** (düşük-orta, novel-hata oto-tespit).
4. **gap-8 drift** (düşük, dead-gate-genişlemesi).
5. **gap-4 anomali (river/S-ESD)** (orta, static-eşik-tamamlayıcı).

Hepsi `emit_event()` çağıran küçük/bağımsız producer'lar → klipper LSA otomatik okur. Her biri ayrı-PR, klipper final-design+Linux-verify.

---

## Ek: kaynak güven-tier

- **✅ bilgi-doğrulanmış:** Drain3 (logpai), eventsourcing (PyPI), Keep (keephq), sentry-sdk/GlitchTip, river, OTel-MCP (traceloop), ReAct-RCA (arXiv 2403.04123), AIOps-survey (arXiv 2406.11213), S-ESD pattern.
- **🔶 adopt-öncesi-verify:** ai-observer (tobilg), dtaianomaly (arXiv 2502.14381), LightESD (arXiv 2305.12266), Bugsink, sql-event-store-spesifik.
- deep-research run: 6-açı / 18-kaynak / 90-iddia; verify-fazı rate-limit (abstain≠refute) → yukarısı knowledge-validate edildi.
