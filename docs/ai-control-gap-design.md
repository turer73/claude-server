# AI-Control GAP Tasarımı — Kontrol Eden Kim?

> **Bağlam:** claude-server 35+ API router'ı, 80+ automation script'i, 2 arkaplan ajanı
> (devops_agent, code_review_agent) ve 1 otonom spawn sistemi (autonomous-claude)
> barındırıyor. AI'ın ne yapmasına izin verildiği ile ne yapabildiği arasında
> **denetim boşluğu (control gap)** var. Bu döküman boşluğu haritalar ve kapatır.
>
> **Temel tez:** Kontrol 3 katmanlı olmalı — GATE (yapamaz) → GUARD (ancak koşulla) → LOG (kanıt).
> Şu an kod tabanında bu katmanlar girift, tutarsız ve bazen hiç yok.

---

## 1. Mevcut Durum: Kontrol Katmanı Envanteri

### Katman 0: Network gate (API'ye kim erişir)

| Mekanizma | Kapsam | Tür |
|---|---|---|
| JWT Bearer + require_auth | 20 router | ✅ Sert (crypto) |
| X-Memory-Key statik header | 6+ router | ⚠️ Yumuşak (tek anahtar) |
| Custom header (X-Pentest-Key) | 1 router | ❌ Ad-hoc |
| **Auth YOK** | **2 router (ws_status, prometheus)** | ❌ AÇIK |
| Import fallback no-op | 1 router (dispatch) | ❌ KRİTİK |

**Boşluk:** dispatch.py'de import fallback'i sessizce auth'u kaldırabiliyor. İki endpoint'te hiç auth yok. Altı endpoint aynı statik anahtarı paylaşıyor — biri kırılırsa hepsi düşer.

### Katman 1: Permission gate (AI ne yapabilir)

| Mekanizma | Kapsam | Tür |
|---|---|---|
| require_admin / require_write | 12 router | ✅ Sert (JWT claim) |
| allowlist (claude-settings.json) | autonomous-claude spawn | ✅ Sert (CLI flags) |
| guardrails.md (system prompt) | autonomous-claude spawn | ❌ Yumuşak (prompt) |
| ShellExecutor whitelist | dispatch.py | ✅ Sert (liste) |
| _INTERP_DENY | dispatch.py | ⚠️ Sert ama denylist (eksik kalabilir) |
| csp.py custom _check_key | 1 router | ❌ Ad-hoc bypass |

**Boşluk:** Guardrails prompt'tur — AI onu atlayabilir (jailbreak, prompt injection). Denylist (yasaklı komut listesi) eksik kalabilir. Yeni bir endpoint eklenirse kontrol mekanizması otomatik eklenmez.

### Katman 2: Rate / Throttle gate (AI ne sıklıkta yapabilir)

| Mekanizma | Kapsam | Tür |
|---|---|---|
| GlobalRateLimitMiddleware | Tüm router'lar (200req/dk) | ✅ Sert |
| autonomous-claude lock file | Otonom spawn | ✅ Sert |
| autonomous-claude throttle (60s) | Otonom spawn | ✅ Sert |
| 14 curl timeout'suz script | External API'ler | ❌ AÇIK |

**Boşluk:** 14 script'te curl timeout yok — script dış API yanıt vermezse sonsuza dek bekler.

### Katman 3: Audit gate (AI ne yaptı?)

| Mekanizma | Kapsam | Tür |
|---|---|---|
| AuditMiddleware | POST/PUT/PATCH/DELETE | ✅ Sert |
| events tablosu (emit_event) | Cron çıktıları, alert'ler | ✅ Sert |
| autonomous-claude spawn log | Otonom spawn | ⚠️ Dosyada, merkezi değil |
| dispatch.py klipper log | CLI dispatch | ⚠️ Dosyada, merkezi değil |

**Boşluk:** Otonom spawnların ve dispatch çağrılarının audit kaydı `events` tablosunda değil, log dosyalarında. AuditMiddleware tüm HTTP isteklerini kaydeder ama AI'ın HTTP DIŞI aksiyonlarını (dosya yazma, git commit, DB sorgusu) kaydetmez.

### Katman 4: Verify gate (AI amacına ulaştı mı?)

| Mekanizma | Kapsam | Tür |
|---|---|---|
| Outcome-contract (cron-wrap) | Cron işleri | ✅ Pilotta |
| _verify_remediation | devops_agent remediation | ✅ Sert |
| health endpoint | Servis sağlığı | ✅ Sert |
| create_discovery dedup | Memory discovery | ⚠️ Düşük coverage |
| create_discovery | Memory tasks | ⚠️ Test eksik |
| MCP tools | Kod çalıştırma | ❌ Test %45 coverage |

**Boşluk:** AI'ın bir aksiyon alması ile o aksiyonun GERÇEKTEN amacına ulaştığını doğrulama arasında boşluk var. Coverage hedefi %80, gerçek %76.6.

---

## 2. Kontrol Boşluğu Haritası

```
┌─────────────────────────────────────────────────────────────────────┐
│                      AI AJAN (Claude / LLM)                         │
└──────────┬──────────┬──────────┬──────────┬──────────┬──────────────┘
           │          │          │          │          │
     [GATE]      [GATE]     [GATE]     [GATE]     [GATE]
    Network    Permission  Rate/       Audit      Verify
               Throttle
           │          │          │          │          │
           ▼          ▼          ▼          ▼          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      claude-server                                 │
│   (API + cron + autonomous agents + dispatch)                       │
└─────────────────────────────────────────────────────────────────────┘

Boşluklar:
┌──────┬─────────────────────────────────────┬──────────┬──────────────┐
│ Kat  │ Boşluk                               │ Etki     │ Aciliyet     │
├──────┼─────────────────────────────────────┼──────────┼──────────────┤
│ GATE │ ws_status + prometheus AUTH YOK     │ Sızıntı  │ KRİTİK       │
│ GATE │ dispatch.py import fallback bypass  │ Bypass   │ KRİTİK       │
│ GATE │ 4 auth pattern, standart yok        │ Güvenlik │ YÜKSEK       │
│ PERM │ Guardrails prompt-only (soft)        │ Jailbreak │ YÜKSEK      │
│ PERM │ Denylist eksik kalabilir             │ Bypass   │ ORTA         │
│ RATE │ 14 script'te curl timeout yok        │ Kilitlenme│ ORTA        │
│ AUDIT │ Autonomous spawn audit events'te    │ Görünmez  │ YÜKSEK      │
│      │ değil, log dosyasında                │          │              │
│ AUDIT │ dispatch çağrıları audit dışı       │ Görünmez  │ ORTA        │
│ VRFY  │ coverage %76.6, test.eksik          │ Güven     │ ORTA        │
│ VRFY  │ MCP/tools.py coverage %45           │ Güven     │ ORTA        │
└──────┴─────────────────────────────────────┴──────────┴──────────────┘
```

---

## 3. Tasarım Hedefleri

1. **Tek tip auth:** Tüm endpoint'ler aynı mekanizmayı kullanır (JWT + require_auth/hierarchy)
2. **Fail-secure:** Auth mekanizması çökerse (import hatası, config eksik) **reddet**, geçme
3. **Katmanlı kontrol:** Gate → Guard → Log → Verify, eksiksiz
4. **Hard override soft:** Guardrails prompt'ta değil, teknik kontrolde
5. **Zero-trust runtime:** AI her aksiyonunda denetlenir, bir kere güvenme
6. **Merkezi audit:** Tüm AI aksiyonları events tablosunda, log dosyasında değil

---

## 4. Önerilen Mimari

```
┌──────────────────────────────────────────────────────────────────┐
│                     AI AJAN KATMANI                               │
│  (Claude interactive / Claude Code / autonomous-claude / API)    │
└──────────────────┬───────────────────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                     GATE KATMANI (genel)                          │
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ Auth        │  │ Rate Limit  │  │ Request ID  │              │
│  │ (JWT+APIKey)│  │ (per token) │  │ (tracking)  │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
└─────────┼────────────────┼────────────────┼──────────────────────┘
          │                │                │
          ▼                ▼                ▼
┌──────────────────────────────────────────────────────────────────┐
│                     PERMISSION KATMANI (router-specific)          │
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │ require_    │  │ require_    │  │ require_    │              │
│  │ admin       │  │ write       │  │ read        │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     GUARD KATMANI (action-specific)               │
│                                                                   │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │ ShellExecutor  │  │ Allowlist/     │  │ Prompt         │     │
│  │ (komut whitelist)│  │ Denylist      │  │ Guardrails     │     │
│  │                │  │ (komut arg)    │  │ (teknik yedek) │     │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     AUDIT KATMANI                                 │
│                                                                   │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │ Audit          │  │ events         │  │ AI Action Log  │     │
│  │ Middleware     │  │ tablosu        │  │ (yeni, TÜM     │     │
│  │ (HTTP istek)   │  │ (cron + alert) │  │  AI aksiyon)   │     │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     VERIFY KATMANI                                │
│                                                                   │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │ Outcome-       │  │ Test Coverage  │  │ Health         │     │
│  │ contract      │  │ Gate (≥%80)   │  │ Endpoint       │     │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 AI Action Log (Yeni Tablo / Yeni Event Türü)

Mevcut `events` tablosundaki event'ler sadece cron/sistem olaylarını tutar.
AI aksiyonları için ayrı bir event türü veya ek kolon:

```
events tablosuna ek:
  - source: "ai" | "system" | "cron" | "user"
  - agent: "devops" | "code_review" | "autonomous" | "dispatch" | "claude_code" | "interactive"
  - action: "shell_exec" | "db_write" | "file_write" | "git_commit" | "api_call" | "classify" | "spawn"
  - target: hedef kaynak (örn. komut, dosya yolu, API endpoint)
  - verified: bool (verify gate geçti mi?)
  - decision: "allowed" | "denied" | "deferred"
```

### 4.2 Gate Standardizasyonu

```python
# app/middleware/gates.py — yeni

# TEK doğru auth fonksiyonu (tüm router'lar kullanır)
def require_auth(request: Request) -> User:
    """JWT + X-API-Key + fail-secure."""
    ...

# Her router zorunlu olarak bunu kullanır
router = APIRouter(dependencies=[Depends(require_auth)])
```

**Tüm router'lar bu pattern'e geçer.** Özel durumlar (public health endpoint'leri) `dependencies=[]` ile açıkça işaretlenir.

### 4.3 Fail-Secure Auth

```python
# dispatch.py — YENİ (import fallback YOK)
# from app.middleware.gates import require_auth  # DIRECT IMPORT
# Fallback YOK — import hatası = 500 hatası = fail-secure
```

**Kural:** Auth import'u başarısız olursa uygulama ayağa kalkmaz (fail-secure). Sessizce no-op olup geçmez.

---

## 5. Uygulama Adımları

### Sprint 1: Kritik Boşlukları Kapat (gün 1-2)

| # | İş | Tahmin | Bağımlılık |
|---|---|---|---|
| 1.1 | ws_status.py → require_auth ekle | 10 dk | — |
| 1.2 | prometheus.py → require_auth ekle | 10 dk | — |
| 1.3 | dispatch.py → fallback kaldır, direkt import | 5 dk | — |
| 1.4 | autonomous-claude.sh:488 → boş key kontrolü | 5 dk | — |
| 1.5 | 14 script'e curl timeout ekle (--max-time 30) | 30 dk | — |

### Sprint 2: Auth Standardizasyonu (gün 3-5)

| # | İş | Tahmin | Bağımlılık |
|---|---|---|---|
| 2.1 | `app/middleware/gates.py` oluştur (tek auth kaynağı) | 30 dk | — |
| 2.2 | csp.py → _check_key'i kaldır, verify_key kullan | 10 dk | 2.1 |
| 2.3 | Tüm router'ları gates.require_auth'e geçir | 1 saat | 2.1 |
| 2.4 | deploy.py JWT auth'a geç (INTERNAL_API_KEY kaldır) | 30 dk | 2.1 |
| 2.5 | security.py dual-header'ı teke indir | 15 dk | 2.1 |

### Sprint 3: AI Action Log (gün 6-8)

| # | İş | Tahmin | Bağımlılık |
|---|---|---|---|
| 3.1 | events tablosuna kolon ekle (source/agent/action/target) | 30 dk | — |
| 3.2 | emit_ai_action() fonksiyonu | 20 dk | 3.1 |
| 3.3 | dispatch.py → her CLI aksiyonu logla | 30 dk | 3.2 |
| 3.4 | autonomous-claude spawn → events'e yaz | 30 dk | 3.2 |
| 3.5 | devops_agent remediation → events'e yaz | 15 dk | 3.2 |
| 3.6 | code_review_agent discoveries → events'e yaz | 15 dk | 3.2 |

### Sprint 4: Guard Güçlendirme (gün 9-12)

| # | İş | Tahmin | Bağımlılık |
|---|---|---|---|
| 4.1 | Guardrails.md → teknik kontrole dönüştür | 1 saat | — |
| 4.2 | _INTERP_DENY → allowlist'e çevir | 30 dk | — |
| 4.3 | ShellExecutor whitelist → tüm dispatch'te zorunlu kıl | 20 dk | — |
| 4.4 | autonomous-claude-settings.json → deny'dan allow'a geç | 1 saat | — |
| 4.5 | Rate-limit per token (JWT claim bazlı) | 1 saat | — |

### Sprint 5: Verify Boost (gün 13-15)

| # | İş | Tahmin | Bağımlılık |
|---|---|---|---|
| 5.1 | AI Action Log'dan haftalık audit raporu | 30 dk | 3.x |
| 5.2 | mcp/tools.py test yaz (coverage %45→%80) | 2 saat | — |
| 5.3 | terminal_manager.py test yaz (coverage %51→%80) | 2 saat | — |
| 5.4 | ws_status.py + prometheus.py test ekle | 30 dk | — |
| 5.5 | dispatch.py auth bypass senaryosu test et | 15 dk | — |
| 5.6 | Coverage gate %80'e çek | — | 5.2-5.5 |

---

## 6. Riskler ve Dikkat Edilecekler

### 6.1 Yanlış Positive (FP) Riski
Gate'leri sıkılaştırmak meşru AI aksiyonlarını da engelleyebilir.
- **Mitigasyon:** Her gate'te "dry_run" modu (logla ama engelleme)
- **Geçiş:** Haftada 1 dry_run sonuçlarını incele, sonra enforce'a geç

### 6.2 Performans Etkisi
Her AI aksiyonunda audit, verify, gate kontrolü ek yük getirir.
- **Mitigasyon:** Asenkron audit (ateşle-unut) + rate-limited verify
- **Benchmark:** Mevcut 200req/dk + global rate limit zaten var

### 6.3 Geriye Uyum
Mevcut sistem (özellikle cron'lar, test'ler) gate'leri atlayarak çalışıyorsa kırılabilir.
- **Mitigasyon:** Önce audit mod (log-only), 1 hafta sonra enforce
- **Rollback:** 1.5'teki değişiklikler kolay revert edilebilir

### 6.4 autonomous-claude Script Dışı
Bazı kontroller shell script'te (curl timeout, boş key kontrolü).
Bunlar Python API'sindeki gate'lerin dışında kalır.
- **Mitigasyon:** Script kontrollerini Python'a taşı veya wrapper ekle

---

## 7. Kabul Kriterleri

- [ ] Tüm endpoint'ler require_auth kullanıyor (ws_status + prometheus dahil)
- [ ] Hiçbir auth mekanizması import fallback'i ile sessizce devre dışı kalamaz
- [ ] dispatch.py auth başarısız olursa fail-secure (500 hatası)
- [ ] Tüm AI aksiyonları events tablosunda kayıtlı (AI Action Log)
- [ ] Guardrails teknik kontrole dönüşmüş (salt prompt değil)
- [ ] autonomous-claude.sh X-Memory-Key boş kontrolü eklenmiş
- [ ] 14 script'te curl timeout var
- [ ] curl timeout'suz script sayısı 0
- [ ] coverage %80'e ulaşmış
- [ ] Ölü 10 script temizlenmiş

---

## 8. Ek: Mevcut vs Hedef Karşılaştırması

| Kriter | Şu An (2026-07) | Hedef |
|---|---|---|
| Auth mekanizması sayısı | 4 | 1 (JWT) |
| Auth'suz endpoint | 2 | 0 |
| Auth bypass riski | dispatch.py import fallback | 0 |
| Soft control (prompt only) | guardrails.md | 0 |
| curl timeout'suz script | 14 | 0 |
| set -e'siz script | 19 | 0 |
| AI action audit | log dosyası | events tablosu |
| Coverage | %76.6 | %80+ |
| Ölü script | 10 | 0 |
