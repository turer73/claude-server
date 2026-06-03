# notify-cron tasarımı — LIVESYS FAZ 3.2 (d) (klipper design-input, JOINT)

**Durum:** TASARIM (deploy YOK). notify-cron riskli=gerçek Telegram → surer-onayına bağlı (wind-down #99763). Bu doküman klipper'ın joint-tasarım girdisi; surer cross-verify + ortak netleştirme sonrası DISABLED-pilot → enable.

## Amaç
FAZ 3.2 (a)(b)(c) ile `events` tablosu KAYIT-ONLY dolu (job-outcome/alert/backup). notify-cron = **TEK auto-notifier**: `events.pending_notifications()` (warn/critical, `notified=0`) → Telegram → `mark_notified`. Mevcut dağınık notifier'lar **atomik retire** (double-notify yok).

## Akış (her ~15-30 dk, klipper-cron-wrap altında = outcome-contract'lı)
1. `pending = pending_notifications()` — zaten var (#18): warn/critical + `notified=0`, `LIMIT 50` (batch-drain, spam-guard).
2. Her event → n8n `klipper-alert` webhook POST. **Payload formatı KORUNUR** (`$json.alert.{source,severity,message,value,threshold}` + `meta`) ki mevcut n8n workflow path'leri kırılmasın.
3. POST başarılı → `mark_notified([ids])` (idempotent; #18'de var). POST fail → mark ETME (sonraki tur retry).
4. OUTCOME marker emit (`pass`/`partial` fetch-fail'de).

## DISABLED-pilot (FAZ2 #16 deseni — güvenli ilk adım)
- `NOTIFY_ENABLED=0` (varsayılan) + `DRY_RUN=1`: pending'i sayar/loglar ("şu N event'i push EDERDİM"), **POST/mark YOK**.
- Enable = surer cross-verify + kullanıcı go (FAZ2-ENABLE deseni).

## ⚠️ ATOMIK retire/exclude (enable anında — double-notify'ı önler)
Envanter TAM (klipper+surer mutual-verify):
1. `klipper-cron-wrap.sh:78` n8n-webhook (job-outcome push) → **RETIRE** (klipper). Enable anında bu POST kaldırılır + notify-cron job-outcome-event'leri aynı payload ile push eder = **taşıma, çift değil**.
2. `backup-monitor.sh` `send_telegram` (lokal-backup, aktif 2x/gün) → **RETIRE/exclude** (FLAG-B, **surer domain**). Ya emit-event'e çevrilir ya `type=backup` notify-cron'dan exclude.
3. `_send_webhook` (devops remediation, 3 call-site) → **KEEP** (remediation≠alert, farklı semantik, event-emit edilmiyor → notify-cron zaten push etmez).

## İlk-enable backlog guard
Enable anında `events`'te birikmiş eski `notified=0` warn/critical olabilir → tek seferde flood. Çözüm: enable-öncesi eski-event'leri `mark_notified` (cutoff) VEYA notify-cron sadece "enable-sonrası" timestamp'leri push etsin. Ortak kararlaştır.

## Açık sorular (surer ile)
- Telegram path: n8n `klipper-alert` webhook mı doğrudan Telegram-bot mu? (mevcut cron-wrap n8n kullanıyor → tutarlılık için n8n.)
- FLAG-A: digest (recent_events) özet-okuması ile notify-cron çakışmaz (digest=özet, notify=acil); ama bridge-sonrası digest'i events'ten okutursak alert çift-gösterimi olmasın — ayrı iş.
- Cron sıklığı: 15 dk mı 30 mu? (kritik-olay gecikme toleransı vs n8n yük.)
