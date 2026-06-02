# Yaşayan Sistem — Mühendislik Planı (LIVESYS-2026-06)

> **Amaç:** Süreklilik + katmanlı otonomi + **tam farkındalık** + regresyon güvenliği,
> orkestra şefi = Claude. Zemin: `docs/system-current-state.md` (doğrulanmış denetim A–H).
>
> **Önceliklendirme (sentezden):** En zayıf halka = Yetenek 3 (farkındalık); tek baskın
> düşman = **sessiz arıza** ("rc=0 ≠ amacını yaptı"). Plan önce bunu hedefler.
>
> **Dürüst sınır:** mutlak sıfır-sapma garanti edilemez; hedef = **sapma görünür+kayıtlı+
> onaylı**. Mekanizma her fazda: definition-of-done + kabul kriteri + blast-radius + rollback + gate.
>
> **Genel kurallar:** (1) önce zemini doğrula, sonra değiştir. (2) küçük/geri-alınabilir adım.
> (3) bir adım doğrulanmadan (test+canlı teyit+onay) sonrakine geçilmez. (4) sapma → DUR →
> Sapma Defteri → karar. (5) hiçbir sessiz kısıtlama (kapsam daralırsa logla).
>
> **Durum:** TASLAK — onay bekliyor. Hiçbir faz onaysız uygulanmaz. Yakın fazlar (0–2)
> detaylı; 3–6 ana hatlı (gelince detaylandırılır).

---

## Definition-of-Done şablonu (her adım için)
`Hedef · Kapsam(+non-goal) · Sahip(klipper/surer) · Kabul kriteri(ölçülebilir) · Blast-radius(neye dokunur) · Rollback · Gate(nasıl doğrulanır)`

---

## FAZ 0 — Acil triyaj & zemin temizliği (mimariden bağımsız, bekleyen gerçek sorunlar)
**Hedef:** araştırmanın çıkardığı güvenlik + bilinen sessiz arızaları kapat; doküman zeminini düzelt.

### 0.1 G-2 — Stale Coolify SSH key temizliği `[surer, VPS]`
- Kabul: root `authorized_keys`'te yalnız [klipperos@klipper] + [github-actions-bilge-english] kalır; 2 Coolify key silinir. · Blast: SSH erişimi. · Rollback: silmeden önce `authorized_keys` yedeği. · Gate: silme sonrası klipper→VPS SSH hâlâ çalışıyor + CI erişimi bozulmadı.

### 0.2 G-1 — Port 22 maruziyeti `[KARAR GEREKLİ → surer]`
- Bulgu: 22 `0.0.0.0` açık; meşru kullanım Tailscale + **public CI** (GitHub Actions, bilge-english). · **Karar forku:** (a) Tailscale-only kısıtla → CI'ı Tailscale'e taşı/SSH-yerine-deploy-webhook, ya da (b) public bırak ama key-only + CrowdSec yeterli kabul + IP-allowlist. · Bu adım önce karar, sonra uygulama. Pubkey-only zaten aktif (kısmi güvence).

### 0.3 G-5 — Backup Uptime-Kuma push ölü host düzeltmesi `[surer/klipper config]`
- Kabul: backup push URL canlı Kuma'ya (klipper Tailscale IP) yönlenir VEYA kaldırılır; başarı/başarısızlık gerçekten izlenir. · Blast: backup monitoring. · Rollback: config geri. · Gate: bir backup sonrası Kuma'da heartbeat görünür.

### 0.4 CLAUDE.md staleness `[klipper, doküman]`
- Kabul: G-3/4/8 + G-7 düzeltilir (klipper 9 container; VPS gerçek 20; OpenClaw/taşınan-servis listesi temizlenir; VPS kernel 6.8.0-110). · Blast: yok (doküman). · Gate: diff review.

### 0.5 #493 — Panola demo seed/gotrue (günlük ~117 E2E fail) `[surer-lead + klipper]`
- Hedef: seed'in 0 kayıt üretip "✅" demesinin kökü (gotrue signUp JSON parse). · Adım: önce **read-only kök-neden** (surer, VPS gotrue/Panola), sonra fix. · Kabul: seed >0 kayıt üretir, Panola E2E demo-reset yeşile döner. · Blast: Panola demo ortamı (prod değil). · Gate: demo-reset E2E pass sayısı ~117 artar. · (Büyükse kendi alt-görevine bölünür.)

**Faz 0 gate:** tüm 0.x kabul kriterleri ✓, Sapma Defteri güncel → Faz 1 onayı.

---

## FAZ 1 — "rc=0 ≠ başarı": iş-çıktısı doğrulama (sessiz arızayı KAYNAKTA öldür)
**Hedef:** her cron/iş çıkış-kodunu değil **amacını gerçekleştirdiğini** doğrulasın.
**Kök sorun:** F-D1 (e2e rc=0 ama 117 fail), demo-seed "✅ 0 kayıt", G-5 (push ölü host) — hepsi "çalıştı" der, "yaptı" demez.

- **PREP BULGUSU (2026-06-01, read-only envanter):** İşlerin ÇOĞU outcome'u **zaten hesaplıyor** (e2e `Failed=`, backup `wc/exit1`, db-retention/nuclei/self-pentest/pull-vps yüksek outcome-sinyali). Eksik = **detection değil PROPAGATION**: e2e-live-test `Failed=3`'ü hesaplıyor ama `rc=0` dönüp logda yutuyor. → Faz 1 işi hafif: sıfırdan kontrol değil, **var olan outcome'u wrapper'a taşı + merkezi kaydet + yüzeye çıkar**.
- **1.1 Outcome-contract konvansiyonu:** her iş bir *başarı yüklemi* tanımlar (çoğu zaten hesaplıyor → standartlaştır). `klipper-cron-wrap.sh` genişletilir: run sonrası yüklemi değerlendirir, **gerçek sonucu** (pass/partial/fail) merkezi tabloya yazar (rc değil). Pilotta işin kendi hesabını (Failed=N) yakalamak yeterli — yeniden-implement yok.
- **1.2 Pilot (3 iş):** test-runner, demo-reset (e2e), backup — en çok sessiz-fail geçmişi olanlar. · Kabul: bu 3'ün "partial" durumu (e2e 3/117 gibi) artık görünür kaydedilir. · Blast: cron-wrap (tüm işleri sarar — dikkatli, geriye-uyumlu olmalı). · Rollback: wrap eski sürüm. · Gate: pilot 3'te bilinen partial-fail'ler doğru "fail/partial" raporlanır; sağlıklı işler "pass" kalır (false-positive yok).
- **1.3 Yaygınlaştırma:** kalan işlere outcome-yüklemi (yüklemsiz işler "tanımsız" işaretlenir — sessiz kapsam yok).

**Faz 1 gate:** pilot kanıtlı + yaygınlaştırma listesi tam (tanımsız kalan işler loglu).

### Faz 1 ilerleme (2026-06-02 — surer ile birlikte, PR #10 MERGED)
- ✅ klipper-cron-wrap.sh outcome-contract: fresh-temp current-run scan + son OUTCOME parse + rc/predicate mismatch-guard + cron_outcomes (server.db) + alert RESULT∈{fail,partial}. Geriye-uyumlu (marker yoksa rc-fallback + outcome-undefined).
- ✅ Pilot emission: test-runner + demo-reset (EXIT-trap; partial-rubrik). **Codex P1 fix:** test-runner predicate RUN_START'a bağlandı (3h-pencere yetersizdi → 3h içinde re-run+abort önceki pass'i gizliyordu).
- ✅ 6 surer-sertleştirme adopte + cross-verify (writer≠verifier): surer PR#10 onay (logic-test 7/7); klipper backup-predicate onay (in-process STAGE → stale-row sınıfına immün).
- 🔶 backup-predicate (surer): dry 10/10 onaylı → wrap canlı (merge) sonrası surer prod'a uyguluyor; ilk gerçek emit birlikte doğrulanacak.
- ⏳ Kuyruk: cron_outcomes retention prune (~90g, db-retention.sh — klipper, düşük öncelik) · e2e-live OUTCOME (wave 1.3) · yaygınlaştırma (kalan işlere predicate).
- ⚠️ Bitişik güvenlik (FAZ1-dışı): backup.sh TELEGRAM_BOT_TOKEN plaintext → surer .env+rotate (kullanıcı onaylı).

---

## FAZ 2 — Doğrulanmış farkındalık: canlılık/tazelik meta-kontrolü (Yetenek 3'ün kalbi)
**Hedef:** sistem her veri kaynağı/işin **canlı + taze + amacını yapıyor** olduğunu bilsin; ölen parçayı kendi fark etsin.
**Kanıt:** CI 39 gün stale (kimse görmedi), otonomi 05-28'den atıl, RAG atıl, backup-push ölü host — hepsi "kaynak öldü, sistem bilmiyor".

- **PREP BULGUSU (2026-06-01) — liveness İKİ sınıf, tek-tip eşik FALSE-POSITIVE üretir:**
  - **(A) Kadans-tabanlı** (cron/poll): staleness eşiği çalışır.
  - **(B) Olay-tetikli / on-demand** (autonomy, alerts, RAG): "eski = arıza" YANLIŞ → atıl/sakin'i arıza sanar (RAG dormant, alerts quiet meşru). Bunlara staleness DEĞİL, "processor canlı mı / girdi-var-çıktı-yok" sinyali gerekir.
  - Taslak registry (prep):

  | Kaynak | Sınıf | Kadans/eşik | Şu an |
  |---|---|---|---|
  | metrics_history | A | 30s / >5dk=ölü | ✓ taze |
  | vps_metrics_history | A | 150s / >10dk | ✓ taze |
  | coverage.db test_runs | A | günlük / >2g | ✓ (CI-staleness zaten dijest'te) |
  | e2e + demo-reset | A | günlük / >2g **+ fail>0** | ✗ F-D1 (Panola ~117) |
  | backup (push+pull) | A | günlük / Kuma heartbeat | ✓ (0.3 fix, yarın teyit) |
  | self-pentest/nuclei | A | haftalık / >8g | ✓ |
  | digest push | A | günlük / yok | ✗ F-C1 (push edilmiyor) |
  | autonomy (ack/spawn) | **B** | event / "input-var-output-yok" | ✗ F-E1 (05-28'den) |
  | rag_queries | **B** | on-demand / "kullanılmıyor"=meşru | F-B2 (atıl, arıza değil) |
  | alerts/notes | **B** | event / processor-heartbeat | sakin |

- **2.1 Kaynak kayıt defteri (registry):** yukarıdaki taslak temel; her kaynağa `sınıf(A/B) + kadans/eşik VEYA heartbeat-tanımı + sahip`. (dijest CI-staleness = A-sınıfı ilk örnek.)
- **2.2 Liveness monitor:** registry'yi tarar, eşik aşan (stale/ölü) kaynakları yüzeye çıkarır. · Kabul: 5 bilinen vaka (CI/otonomi/RAG/backup-push/e2e) sentetik olarak eski yapıldığında **hepsi yakalanır**; taze olanlar alarm üretmez (false-positive disiplin). · Blast: yeni okuma-katmanı (yazma yok). · Gate: 5/5 yakalama + 0 false-positive.
- **2.3 Çıktı:** dijest + alert'e bağlanır (Faz 3 ile).

**Faz 2 gate:** 5/5 sentetik yakalama kanıtlı.

---

## FAZ 3 — Olay omurgası + dijest push (farkındalık ÇIKTISI bağlanır)
**Hedef:** F-C1 kapat (dijest gerçekten push edilsin) + aksiyonlar kendini *duyursun*.
- 3.1 Dijest scheduler (cron/systemd-timer) + has_signal guard'ı (zaten var). 
- 3.2 Hafif olay omurgası: anlamlı aksiyon (deploy/fix/job-outcome/alert) → merkezi olay kaydı + ilgili tarafa bildirim. Mevcut `/monitor/webhooks` + notes-kanalı (klipper↔surer zaten kullanıyor) genelleştirilir. *Claude'u kalp-atışı yapma; deterministik kayıt + eşik.*
- DoD/kabul: faza gelince detaylandırılır.

## FAZ 4 — Blast-radius / değişiklik-öncesi etki (Yetenek 4'ün proaktif yarısı — F-F1)
**Hedef:** bir değişiklik neye dokunduğunu ÖNCEDEN bilsin (şu an elle grep). Hafif bağımlılık/etki haritası (route↔DB↔consumer). Ana hatlı; gelince detay.

## FAZ 5 — Kapalı-döngü otonomi (Yetenek 2 — F-E1/E2)
**Hedef:** otonom aksiyon kendi sonucunu doğrulasın, başarısızsa rollback/eskale. Atıl sınıflandırma + dormant remediation canlandırılır + doğrulama eklenir. Ana hatlı.

## FAZ 6 — Orkestra rolü/sınır (Yetenek 5)
**İlke (build değil, sınır):** kalıcı/tekrarlayan = deterministik kod; Claude = büyük/belirsiz/yargı kararlarının şefi, olay-omurgasıyla tetiklenir — **kalp atışı değil**. Bu, planın sonunda netleşir.

---

## Sapma Defteri (plan yürütme)
| Tarih | Faz/Adım | Sapma | Karar |
|---|---|---|---|
| 2026-06-01 | 0.4 | surer'ın CLAUDE.md sapmaları (G-3/4/8/7: klipper "2"→9, "17"→20, OpenClaw, kernel) **stale-premise** — disk CLAUDE.md zaten doğru (9 aktif / 20 konteyner / OpenClaw yok). surer eski/cached CLAUDE.md okumuş. | Körlemesine düzeltme YAPILMADI; sadece VPS audit tarihi 06-01'e çekildi. 0.4 ≈ no-op (doğrulandı). |
| 2026-06-01 | 0.5/#493 | #493 kök-neden **3 kez değişti**: (v1 surer) auth-path → (klipper) tenant-RLS-membership → (v2 surer) sema-divergence customers/ownership. SONRA klipper 3-kaynak çapraz-kontrol: **app src/ contacts+tenant_id(318ref, prod'da çalışıyor) + migrations contacts/tenant** ↔ surer introspect customers/ownership. **Tek DB ikisini birden olamaz** → muhtemelen surer yanlış DB introspect etti VEYA E2E-DB prod'dan ayrı/stale. | **Rewrite DURDURULDU** (her iki yön de yanlış olabilir). surer :8080'in gerçek DB'sini re-verify ediyor. (a) DB-fix iptal (surer doğru durdurdu). (b) fail-loud commit'li (bf6e9a9) → sessiz maske bitti. Fix yönü = mimari karar (seed mi / E2E-DB migrate mi / repoint mi), netleşince kullanıcıya. |
| 2026-06-01 | araç | pre-bash-guard hook "HALT" kelimesini `halt` shutdown sanıp note POST'unu blokladı (FP). | Kelime değiştirildi (DUR); not gönderildi. Küçük hook-FP, kayda geçti. |
| 2026-06-02 | 0.5 | #493 B1/B2 deliberasyonu (surer :8080-merkezli + plan doc + benim AskUserQuestion B1-forku) **çok-katmanlı stale-premise**: kullanıcı o gece seed'i zaten cloud'a rewire etmişti (commit 4eb85d5, unpushed). B1-rebuild gereksizdi. | B1-rebuild'e başlamadan ÖNCE seed git-log + .env okundu → çelişki yakalandı, DUR + kullanıcıya rapor. Read-only cloud check yeşil → B1 iptal, sadece cron export fix. Ders: agir eylem öncesi gerçek state (git/.env/cloud) doğrula. |
| 2026-06-02 | PR#9 | test fail için ilk kök "noexec tmp" hipotezi (noexec tmpfs ile semptom tekrarlandı ama yanlıştı). | `assert ..., resp.text` ekleyip CI'dan gerçek hata alındı → asıl kök `ROOT` hardcoded spawn cwd CI'da yok (ENOENT). Spekülatif noexec kodu geri alındı. Ders: semptom-tekrarı ≠ kök-doğrulama. |

### Faz 0 ilerleme
- ✅ 0.1 G-2 stale key sil — surer uyguladı, **klipper bağımsız gate PASS** (2 doğru key, CI sağlam).
- ✅ 0.3 G-5 backup push — surer host-only fix, **bağımsız doğrulandı** (eski=000 ölü, yeni=302); canlı heartbeat yarın 03:00 doğal run'da.
- ✅ 0.4 CLAUDE.md — doğrulandı (zaten doğruydu), audit tarihi güncellendi.
- ✅ 0.2 G-1 port22 → **UYGULANDI + bağımsız doğrulandı**: drop-in `00-hardening.conf` (PasswordAuthentication no first-wins + MaxAuthTries 3 + LoginGraceTime 20); runtime passwordauth=no (cloud-init gölge bug kapandı), pubkey/CI sağlam, lockout yok. Port22 public (kullanıcı kararı) + CrowdSec aktif-ban.
- ✅ 0.5 #493 — **ÇÖZÜLDÜ 2026-06-02** (115 fail/8 → **119/123**, auth-flow 4/4). B1/B2 deliberasyonu eskiydi: kullanıcı 2026-06-01 gecesi seed'i ZATEN cloud'a rewire etmişti (panola bf6e9a9 + 4eb85d5; .env zaten cloud + service-role). Yeni seed tenant-scoped + hard guard (742dbb7e) → cloud-prod'da bile sadece demo tenant'a yazar (B2 prod-wipe çekincesi çözülür). Read-only cloud check yeşil + manuel seed başarılı (30 müşteri vb.) + E2E doğruladı. **Klipper fix:** `demo-reset-test.sh` `E2E_SUPABASE_SERVICE_ROLE_KEY` export gap (cron yeni seed'i boş key'le fail ederdi) → eklendi. 3 residual ayrı panola işi (stock test-selector-drift + production tenant-resolution race), #493 kökünden bağımsız. Detay: memory `fix-panola-493-resolved-cloud-2026-06-02`.

**Faz 0 KAPANDI** (0.1–0.5 ✅). Sapma: #493 = planın poster-child'ı oldu (sessiz başarı ikinci bug'ı sakladı) AMA ayrıca çok-katmanlı stale-premise dersi verdi (memory `correction-stale-premise-493-2026-06-02`). → Faz 1 onayı bekliyor.

## Açık kararlar (uygulamadan önce kullanıcı)
- ~~0.2 Port 22~~ → KARARLAŞTI: public + pubkey-only + CrowdSec (kullanıcı kabul).
- **CANLI KARAR: Faz 0 kapandı → Faz 1 onayı bekliyor** (outcome-contract / "rc=0 ≠ başarı"). Pilot 3 iş: test-runner, demo-reset, backup.
- Faz sırası/durdurma noktaları kullanıcı onayına tabi.

## Faz 0 sonrası açık kuyruk (plan-dışı / küçük)
- 0.3 backup heartbeat: push fix doğrulandı (302); canlı Kuma heartbeat 03:00 doğal run'da teyit edilecek (henüz açık).
- ✅ **Dijest push (F-C1) — YAPILDI 2026-06-02**: `automation/digest-send.sh` + crontab `0 8 * * *` (klipper-cron-wrap, NOTHING_NEW guard). Uçtan uca doğrulandı (gerçek Telegram push, exit 0). Faz 3.1'in bir parçası erken kapandı.
- 3 panola E2E residual:
  - ✅ **05-stock** — test-selector-drift düzeltildi (kart layout), pass. Commit d033875 (local, push bekliyor).
  - 🔶 **03+10 production** — gerçek prod-auth race fix'i (TenantContext session-guard + auth-change refetch) commit df50cda (local); 899/899 unit gate temiz; **canlı-race E2E teyidi panola.app DEPLOY sonrası** (push onayı bekliyor — prod-app davranış değişikliği).
