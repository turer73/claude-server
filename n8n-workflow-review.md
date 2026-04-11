# Klipper System Auto-Repair Pipeline

**Proje:** Linux-AI Server otomatik sorun tespit ve duzeltme sistemi  
**Tarih:** 2026-04-11  

---

## Mimari

```
DevOps Agent (30s aralik)
  |
  +-- Sorun tespit edildi
  |     |
  |     +-- Telegram: "Sorun var" (anlik)
  |     +-- n8n webhook: alert_detected (anlik)
  |     +-- Playbook calistir (otomatik fix)
  |     +-- 15s bekle
  |     +-- Dogrula (metrik/servis/container kontrolu)
  |     +-- Telegram: "Duzeltildi" veya "Duzeltilemedi"
  |     +-- n8n webhook: fix_result
  |
  +-- n8n Workflow (paralel)
        |
        +-- Alert al (webhook)
        +-- Fix komutu belirle (source'a gore)
        +-- Shell exec ile uygula
        +-- 15s bekle
        +-- verify_fix API ile dogrula
        +-- Telegram: durust sonuc bildirimi
        +-- Event log'a kaydet
```

**Neden iki katman?** DevOps agent hizli (yerel, 30s). n8n ikinci goz (bagimsiz dogrulama). Biri basarisiz olursa digeri devam eder.

---

## Degisiklikler

### 1. `app/core/config.py`
Yeni ayarlar:
- `telegram_bot_token` — Telegram Bot API token
- `telegram_chat_id` — Bildirim gonderilecek chat ID
- `n8n_webhook_url` — n8n webhook URL (varsayilan: `http://localhost:5678/webhook/system-alert`)

### 2. `app/core/devops_agent.py`
Yeni metodlar:
- `_notify_telegram_alert()` — Sorun tespitinde Telegram bildirimi
- `_notify_n8n()` — Sorun tespitinde n8n webhook'a POST
- `_verify_fix()` — Remediation sonrasi durust metrik/servis/container kontrolu
- `_notify_telegram_fix_result()` — Fix sonucunu Telegram'a bildir
- `_notify_n8n_fix_result()` — Fix sonucunu n8n'e bildir

Degisiklikler:
- `_detect()` — Yeni alert olusturuldugunda Telegram + n8n bildirim
- `_remediate()` — Playbook sonrasi 15s bekle + dogrula + sonuc bildir
- `_remediate_service()` — Ayni pipeline: bildir → duzelt → dogrula → raporla
- `_remediate_container()` — Ayni pipeline: bildir → duzelt → dogrula → raporla

### 3. `app/api/webhooks.py`
Yeni action: `POST /api/v1/monitor/webhooks/trigger/verify_fix`
- Body: `{"alert_source": "cpu"}` veya `"service:nginx"` veya `"docker:n8n"`
- Metrik/servis/container kontrolu yapar
- Durust degerlendirme doner: `{"fixed": bool, "detail": "...", "honest_assessment": "..."}`

### 4. `n8n-workflows/system-auto-repair.json`
n8n workflow (import edilecek):
- Webhook Trigger → Alert analiz → Fix komutu belirle → Shell exec → Bekle → Dogrula → Telegram sonuc

---

## n8n Workflow Akisi

```
Receive Alert (POST /webhook/system-alert)
  |
  +-- Respond OK (hemen)
  +-- Is New Alert? (event == "alert_detected")
        |
        +-- Determine Fix (Code node — source'a gore komut belirle)
        |     cpu → ps aux | head
        |     memory → docker prune + pip cache + tmp clean + page cache drop
        |     disk → docker prune + log truncate + journal vacuum
        |     temperature → CPU governor powersave
        |     service:X → systemctl restart X
        |     docker:X → docker start X
        |
        +-- Has Fix Commands?
              |
              YES → Split Commands → Loop → Execute Fix → Wait 2s → Loop
              |       (tum komutlar bitince)
              |       ↓
              +----→ Wait 15s (fix etkisi icin)
                      ↓
                    Verify Fix (POST /trigger/verify_fix)
                      ↓
                    Is Fixed?
                      |
                      YES → Telegram "Duzeltildi"
                      NO  → Telegram "Duzeltilemedi — Manuel mudahale gerekli"
                      |
                      +→ Log Result (webhook event kaydi)
```

---

## Kurulum

### 1. Env degiskenleri (.env)
```bash
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
N8N_WEBHOOK_URL=http://localhost:5678/webhook/system-alert
```

### 2. n8n workflow import
1. n8n'i ac (http://localhost:5678)
2. Workflows → Import from File
3. `n8n-workflows/system-auto-repair.json` sec
4. Workflow'u aktif et (toggle)

### 3. n8n env degiskenleri
docker-compose.n8n.yml'de zaten var:
```yaml
LINUX_AI_API_KEY=${DEFAULT_API_KEY:-}
```
Ek olarak Telegram icin:
```yaml
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}
```

### 4. Sunucuyu yeniden baslat
```bash
sudo systemctl restart linux-ai-server
```

---

## Durust Degerlendirme

### Calisacak Olanlar
| Senaryo | Calisiyor mu | Neden |
|---------|-------------|-------|
| CPU yuksek → top process tespiti | ✅ Evet | `ps aux` her zaman calisir |
| Memory yuksek → Docker prune + cache temizleme | ✅ Evet | Genelde 5-15% bellek kazandirir |
| Disk dolu → Docker prune + log truncate | ✅ Evet | Docker image/layer temizligi etkili |
| Servis durdu → systemctl restart | ✅ Evet | Dogrudan ve guvenilir |
| Container durdu → docker start | ✅ Evet | Dogrudan ve guvenilir |
| Telegram bildirim | ✅ Evet | Resmi API, guvenilir |
| Post-fix dogrulama | ✅ Evet | Gercek metrik kontrolu |

### Calismayabilecekler
| Senaryo | Risk | Aciklama |
|---------|------|----------|
| CPU surekli yuksek | Orta | Playbook sadece loglama yapiyor, process kill etmiyor |
| Disk tamamen dolu | Dusuk | Docker prune calisabilir ama yer acmayabilir |
| n8n kendisi cokerse | Dusuk | DevOps agent yine Telegram'a bildirir, n8n workflow calismaz |
| Telegram API erisim yok | Dusuk | Internet kesintisinde bildirim gitmez |

### Mimari Sinirlar
- **Otomatik process kill yok** — Guvenlik nedeniyle, CPU yuksek durumunda sadece log alinir
- **Veritabani onarimi yok** — SQLite bozulursa manuel mudahale gerekir
- **Kernel modulu sorunlari** — Kernel panic/module crash icin cozum yok
- **Ag sorunlari** — wifi-watchdog.sh zaten bunu yapiyor (ayri cron)
