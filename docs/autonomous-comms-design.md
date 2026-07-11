# Faz-A: Otonom-Haberleşme Genişletme — Notes-Schema + Güvenlik-Çekirdeği (Tasarım)

> **Lane:** design=klipper, impl=bölüşülür (schema+güvenlik-çekirdeği=klipper, surer-taraf-parite=surer).
> **Durum:** taslak, surer spec-verify + Turgut onayı bekliyor.
> **Bağlam:** Turgut 2026-07-05'te "klipper ile daha kapsamlı otonom haberleşme kuralım" dedi. surer #100447
> ile 4 boşluk + fazlı-öneri (A-D) gönderdi, 12-madde mutabakat #100447→#100472/#100466/#100469 zincirinde
> kapandı (2026-07-05, 0 açık-itiraz). Şimdi (2026-07-11) P0-kimlik (PR#302, per-device-key) + P1-CLAIM-lock
> (PR#303) canlı — bu ikisi Faz-A'nın **önkoşul-katmanı**, asıl teslimat (bu doküman) henüz yapılmadı.

## 0. Çözülen 4 Boşluk (surer #100447)

1. **Otonom-diyalog-zinciri kopuk** — surer headless-spawn'dan not gönderemiyor (blanket-ban).
2. **Teslimat-semantiği yok** — fire-and-forget, ACK/retry/state-machine yok.
3. **Güvenlik-asimetrisi** — surer `headless_spawn.ps1` izolasyonsuz (F:/ doğrudan, timeout-yok).
4. **Gecikme** — 60s/30s poll-cadence.

## 1. Fazlar

| Faz | Kapsam | Lane | Bağımlılık |
|---|---|---|---|
| **A** | notes-schema (thread_id/reply_to/hop_count/msg_type) + claims/audit-tablo + state-machine | klipper | — |
| **B** | surer-spawn izolasyon-paritesi (worktree+timeout+kısıtlı-settings) | surer | — |
| **C** | sınırlı-otonom-diyalog (flag default-off, hop-TTL) | ortak | **B tamamlanmadan ship edilemez** (bkz §2) |
| **D** | long-poll (opsiyonel, gecikme-azaltma) | — | düşük-öncelik |

**KİLİT-KARAR (klipper design-position, değişmez):** Faz-B, Faz-C'yi **gate'ler** (paralel değil).
Faz-C otonom-diyalog, Faz-B izolasyon-paritesi test-kanıtıyla BİTMEDEN flag açılamaz — aksi halde
[[feedback_spawn_contamination_pre_pr_check]] KİRLİLİK-4 dersini Windows tarafında tekrar yaşarız.
**B-DONE = test-kanıtı, bildirim değil** (surer #100450 eki): collision-regression testi
(headless-spawn-commit → F:/ ana-repo-HEAD değişmez) + timeout-testi + kısıtlı-settings-doğrulaması
guard-varlık fail-closed deseninde olmalı, honor-system değil.

## 2. Notes-Schema (Faz-A çekirdek teslimatı)

Mevcut `notes` tablosuna 4 yeni sütun, backward-tolerant migration:

```sql
ALTER TABLE notes ADD COLUMN thread_id INTEGER;      -- NULL = tekil-not (eski-davranış, thread-dışı)
ALTER TABLE notes ADD COLUMN reply_to INTEGER;        -- NULL = thread-başlatıcı; not-in FK (SQLite'ta zorunlu-değil, app-level doğrulanır)
ALTER TABLE notes ADD COLUMN hop_count INTEGER DEFAULT 0;   -- otonom-cevap-zinciri derinliği
ALTER TABLE notes ADD COLUMN msg_type TEXT DEFAULT 'legacy'; -- 'dialogue' | 'dispatch' | 'legacy' (server-türetilir, bkz §3)
```

**Migration-compat (surer #100450 eki, KABUL):** eski-istemci (note-poller.ps1, mevcut hook'lar)
yeni-alansız `POST /notes` çalışmaya devam etmeli — server-default `thread_id=NULL/hop_count=0/
msg_type='legacy'` uygular, kademeli-geçiş (big-bang değil). `note-poller.sh`'taki
`status`-kolonu `pragma_table_info`-guard precedent'i (satır 63-67, idempotent-ekleme deseni —
bu oturumda `_ensure_read_by`/`_ensure_verified`/`device_keys` hep aynı desen) yeni-kolonlara da
uygulanır: taze/pre-migration DB'de poller kırılmaz.

## 3. Sunucu-Otorite Üçgeni (EN KRİTİK — #3+#7, klipper+surer yakınsaması)

Üç alan da **server-side türetilir**, client-iddiası asla kabul edilmez, bilinmeyen-durum
**fail-CLOSED** ([[feedback_unforgeable_server_side_enforcement]]):

1. **`msg_type`** — client sadece *ipucu* verir (`hint`), server karar verir. Kural: içerik
   `action_review`-yakalı bir aksiyon-tetikliyorsa (task-paketi/dispatch-komut/spawn-emri) →
   `dispatch`(consequential); aksi halde `dialogue`. Bilinmeyen/parse-edilemeyen → `dispatch`
   (fail-closed, asla `dialogue`'a düşme).
2. **`sender`** — `from_device` DEĞİL (body-iddiası, spoofable — X-Memory-Key şu an paylaşımlı-master
   tarafından da kabul ediliyordu, P0-kimlik'ten önce). Artık: **per-device-key**'den türetilir
   (`_resolve_device_key`, PR#302 CANLI) — `dispatch_origin` deseni discussions/claims'te zaten
   bu şekilde çalışıyor, notes-thread'lere de AYNEN uygulanır.
3. **`action-surface`** — dialogue-mesajı `action_review` (docs/gap1-action-review-design.md)
   ile aynı deterministik-tarama'dan geçer; consequential-içerik tespit edilirse `msg_type`
   zorla `dispatch`'e yükseltilir, hop-TTL'den bağımsız insan-gate'e düşer (bkz §4).

## 4. hop-TTL ⊥ Policy-Gate

**"Otonomlaşan KONUŞMA, KARAR değil."** `held`-sınıfı (policy-gate #1222/#1342-44 ile aynı-aile,
consequential dispatch) hop-1'de bile insan-gate'te durur — hop-count'tan bağımsız. hop-TTL
yalnız `dialogue`-tipi mesajlar için "N hop sonra otomatik-dur" anlamına gelir; `dispatch` asla
hop-TTL'e güvenerek otomatik-akmaz.

**Invariant-test (surer #100450 eki):** `held`-not + `hop_count>0` → yine `HOLD` (hop-TTL
held-bypass DEĞİL). CI'da adversarial-tablo ile (bkz §10) doğrulanır.

## 5. Kill-Switch (2-katman, #1 — EN ÖNEMLİ tek-madde, klipper 6-ekleme #1)

hop-TTL kendiliğinden-durur ama insan-görünür **thread-audit + manuel-acil-durdurma** yoktu.
Bu, **Faz-B-gate'iyle eşdeğer öncelik**: Faz-C flag'i açılmadan bu ikisi hazır olmalı.

1. **DB-halt-flag** — yeni `autonomous_comms_halt` tablosu (tek-satır, `active INTEGER`).
   Poller her-tick spawn-ÖNCESİ okur; `active=1` ise o thread/tüm-sistem için spawn atlanır.
2. **Thread-kill** — process-ağacı seviyesinde: Windows `taskkill /T` (surer-tarafı),
   Linux `pkill -TERM -P <pid>` (klipper-tarafı, `_spawn-worktree-lib.sh`'taki mevcut
   timeout-wrapper deseniyle aynı aile). `claude -p` node-çocukları dahil.

Dashboard'da thread-view (yeni `/api/v1/agents/comms-threads` endpoint) + tek-tuş kill-switch —
acil-bütçe-freni.

## 6. Poison/DLQ (#2, klipper-ekleme #2)

`autonomous-spawn-retry.sh` + `spawn_failures` tablosunun (bugün #1297'de SQL-injection-guard'ı
fixlenen POISON_THRESHOLD deseni) **aynısı** thread-state-machine'e taşınır: max-retry + max-thread-age
(wall-clock TTL, CLAIM'in 4h-stale-deseniyle aynı-aile) → `FAILED`/`poison` state → insan-yüzeyine
(dashboard, agent-health-report). DLQ→§7 (bütçe) ile bağlı: poison-thread bütçe-tüketmeyi durdurur.

## 7. Global Bütçe-Tavanı (#4, klipper-ekleme #4)

Per-thread limitlerin ÜSTÜNDE, **tek-kaynak DB-sayaç** (`autonomous_comms_budget`, günlük-reset):
eşzamanlı-spawn sayısı + günlük-token-tavanı. Her iki lane (klipper+surer) spawn-öncesi decrement
eder — N-thread × claude-p patlaması riskini keser. Tavan-davranışı `msg_type`'a göre: `dispatch`
(consequential) tavan-aşımında **reddedilir + insan-bildirim**; `dialogue` tavan-aşımında **defer**
(sıraya-alınır, kayıp-değil).

## 8. Idempotency (#5, surer-düzeltme KABUL)

Anahtar: **`(thread_id, note_id)`** — surer'in düzeltmesi (msg-hash değil, çünkü aynı-içerik
farklı-amaçla tekrar-gönderilebilir, note_id zaten unique+monoton). 2-katman: **teslimat**
(poller aynı note_id'yi 2 kez işlemez — bu oturumda CANLI-KANIT: mark-read-atlama →
çift-aksiyon riski yaşandı) vs **aksiyon** (aynı note işlense bile ürettiği spawn/dispatch
tekilleştirilir, `processed_notes(thread_id, note_id)` unique-index).

## 9. Thread-Serialization (#8)

Spawn-lock **`note_id→thread_id` bazlı**: aynı thread'de aynı-anda yalnız 1 spawn in-flight
(worktree-nonce zaten dosya-çakışmasını önlüyor — bu KISMİ, ayrı-katman; thread-serial mantık-sırasını
garantiler, worktree fiziksel-izolasyonu garantiler, ikisi birlikte gerekli).
İlgili: [[feedback_spawn_task_same_dir_git_crosstalk]].

## 10. Append-Only Audit (#12, surer-eklemesi — SUBSTRAT)

Yeni `autonomous_comms_audit` tablosu: thread-history + routing-kararları + red/held-mesajlar
server-side **append-only** — spawn/agent hiçbir satırı EDİTLEYEMEZ/SİLEMEZ (yalnız service-role
INSERT). [[reference_spawn_guard_bash_redirect_gap]]'teki settings-tampering dersiyle AYNI-SINIF:
spawn-write-guard kapsamına audit-tablo da girer. **Bu tablo diğer her-şeyin substratı:**
§11 shadow-precision buradan ölçülür, §5 kill-switch buradan-okur (denetim-izi), §3
server-otorite-üçgeni buraya provenans yazar → **Faz-A şemasında audit-tablo İLK tasarlanır,
diğerleri ona referans verir.**

## 11. Güvenli-Devreye-Alma: Shadow-Rollout + Dry-Run-CI (#10+#11)

**#10 (klipper, kademeli-rollout):** Faz-C binary-flip DEĞİL — enforcement-ladder
(docs/g6-enforcement-ladder-design.md'deki G6 recommend-only→insan-FLIP deseninin aynısı).
Faz-0: otonom-cevap ÜRETİLİR ama her-zaman HELD (asla otomatik-gönderilmez), precision
shadow-ölçülür → terfi Turgut'un gate-FLIP-kontrolünde. Ground-truth 2-metrik (surer
pekiştirme): routing-doğru / cevap-kabul (G4-mark'ın kopyası). FLIP-kriteri **kantitatif-
önceden** belirlenir (G6'daki N-firing+precision-eşik deseni). **Shadow-maliyet-notu:**
held-cevap bile `claude -p`-spawn ÜRETİR → §7 bütçe-tavanı shadow-modda DA geçerli
(zaman-kutulu/örneklemeyle sınırlanabilir).

**#11 (routing saf-fonksiyon + adversarial-CI):** `route(msg_type, hop_count, thread_state, gate)
→ {dialogue|held|rejected}` — **spawn-yok**, test-edilebilir saf-fonksiyon. CI'da adversarial-tablo:
forged-msg_type, hop-overflow, consequential-kılığında-dialogue — hepsi `rejected`/`held` vermeli.
`AUTONOMOUS_MODE=0` ile dry-run edilebilir (CI-coverage, spawn-network-yok).

## 12. SPOF (#6, klipper-ekleme #6, surer-düzeltme: asimetrik)

Kanal = klipper-HTTP-API → `claude_memory.db`. klipper-down = surer izole. **Asimetrik
store-and-forward:** yalnız `dialogue` (non-consequential) için surer-tarafında local-kuyruk +
retry. `dispatch` (consequential) **FAIL-FAST** — gecikmiş-replay tehlikeli (klipper geri-gelince
eski-bir-dispatch'in bugünkü-bağlamda çalıştırılması riski). Dokümante-edilecek: surer kendi
store-and-forward implementasyonunu Faz-B ile birlikte yazar.

## 13. Faz-A Kapsamı-DIŞI (Faz-C+ konuları)

- **#9 semantik-loop (livelock guard, hop-TTL-ötesi):** iki-ajan konu-döngüsünde hop-limitin
  altında ama anlamsal-olarak sonsuz-tekrara girmesi (örn. "anladım"/"tamam" ping-pong'u).
  Faz-C'de not-düzeyinde ele alınır, Faz-A'nın şema/güvenlik-çekirdeğine bağımlı değil.

## 14. Açık Kararlar (Turgut)

1. Faz-A'nın gerçek-önceliği: bugünkü tartışma-platformu (PR#305, discussion#1/#2'de canlı
   kullanıldı) YAPILANDIRILMIŞ-deliberasyon için Faz-A'nın çözmeye çalıştığı "otonom-diyalog-kopuk"
   sorununu KISMEN karşılıyor olabilir — kapsam daraltılsın mı (yalnız §3+§5+§10 çekirdek-güvenlik,
   ham-diyalog-zinciri değil), yoksa tam-12-madde mi?
2. Sıralama: NVIDIA-router (P0-P1 fix'leri, bugün aktif) ile Faz-A hangisi önce?
3. Impl-lane bölüşümü onayı: şema+güvenlik-çekirdeği(§2-§10)=klipper, surer-parite(Faz-B)=surer,
   zamanlama koordineli mi ayrı mı?

---
**Kaynak-notlar:** surer #100447/#100450/#100466/#100469, klipper #100449/#100461/#100467/#100472.
Memory: `project_autonomous_comms_design_2026_07_05.md` (12-madde mutabakat tam-metin),
`project_session_2026_07_11_pr306_310_nvidia_router_honest.md` (bugünkü doğrulama: schema hiç
yapılmamıştı, bu doküman o boşluğu kapatır).
