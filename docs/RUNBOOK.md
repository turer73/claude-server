# Klipper RUNBOOK — Operasyonel Müdahale Rehberi

Alarm geldiğinde / sistem fail olduğunda hangi komutu çalıştırırsın, hangi log'a bakarsın.
Son güncelleme: 2026-05-27 (sustained-N + temp sensor fix sonrası).

## Hızlı sistem bilgisi
- **Host:** klipper (Beelink SER8, Ryzen 7 8845HS, 28GB RAM, 98GB SSD, Ubuntu 26.04)
- **Tailscale:** `100.84.251.49` (yeni), eski `100.113.153.62` offline kalıntı
- **API:** `http://localhost:8420` (FastAPI uvicorn, 2 worker)
- **systemd unit:** `linux-ai-server.service`
- **Cron user:** `klipperos` (NOPASSWD sudo)
- **VPS:** Contabo `100.126.113.23` Dokploy, `/api/v1/vps/exec` ile SSH

---

## 1. Telegram alarm geldi → ne yap

### A) "🚨 Klipper Uyari" — threshold breach (sustained 3x 5dk = 15dk)

Alarm `severity=warning` veya `critical` ile gelir. Mesajda `primary_metric`, `value`, `threshold` var.

**Hızlı tanı (önce SSH/Tailscale ile bağlan):**
```bash
# Anlık metric — yeni alarm sebebi devam ediyor mu?
curl -s -H "X-API-Key: $(grep ^INTERNAL_API_KEY /opt/linux-ai-server/.env | cut -d= -f2)" \
  -X POST http://localhost:8420/api/v1/monitor/webhooks/trigger/metrics_snapshot \
  -d '{}' -H 'Content-Type: application/json' | python3 -m json.tool

# Sustained history — son 3 ölçüm
cat /var/lib/linux-ai-server/alert-state/*.history

# Son 30dk alarm log
tail -30 /var/log/linux-ai-server/alerts.log
```

**Senaryo: CPU yüksek (sustained 90%+)**
```bash
# Hangi process yiyor?
top -bn1 | head -20
ps -eo pid,user,%cpu,%mem,cmd --sort=-%cpu | head -10

# Sebepler genelde:
#  - test-runner (cron 06:00, ~30s burst, normal)
#  - demo-reset playwright (cron 04:00, ~5dk burst)
#  - otonom Claude spawn (data/hook-logs)
#  - n8n workflow execution
#  - Ollama model loading

# Otonom claude spawn aktif mi?
ls -lt /opt/linux-ai-server/data/hook-logs/ | head -5

# Geçici çözüm: spam veriyorsa eşik geçici yükselt
sed -i 's/^T_CPU=.*/T_CPU=95/' /opt/linux-ai-server/automation/alert-check.sh
```

**Senaryo: TEMP yüksek (CPU k10temp 80°C+)**

⚠️ **ÖNCE YAZILIM denetle, DONANIM ŞÜPHESİ EN SON.** Klipper idle temp 35-45°C (k10temp), 80°C+ sustained = process saturate, donanım çok nadiren. 2026-05-27 incident'i: 11h zombie ugrep `/` scan 84°C'ye çıkardı.

```bash
# 1) ZOMBI PROCESS KONTROL (en sik sebep)
ps -eo pid,user,%cpu,etime,cmd --sort=-%cpu | head -10
# %100+ CPU + uzun etime = zombi. Kill -9 ile öldür.
# Özellikle dikkat: grep -r / find / sort / xz multi-thread paralelize ederse

# 2) Tüm sensorler (yazılım sebep değilse)
for h in /sys/class/hwmon/hwmon*; do
    name=$(cat "$h/name" 2>/dev/null)
    for t in "$h"/temp*_input; do
        [ -f "$t" ] && val=$(($(cat "$t")/1000))
        echo "$name/$(basename $t): ${val}°C"
    done
done | grep -v ': 0°C'

# 3) Throttling olmuş mu?
dmesg --since '1 hour ago' | grep -i 'thermal\|throttle'
sudo turbostat --quiet --num_iterations 1 --interval 1 | head -3

# 4) Donanım şüphesi (yukarıdaki 3 adım temizse):
#    - Idle %5 CPU ama temp 70°C+ = cooling problemi (toz/pasta/fan)
#    - Throttle event var = thermal sınır + cooling yetersiz
#    - Beelink SER8 fan BIOS-only kontrol, host'tan PWM ayarlayamazsın
```

**Senaryo: DISK doldu (>90%)**
```bash
df -h /
du -sh /opt /var /data /home /tmp 2>/dev/null | sort -h

# En sik suclular:
du -sh /var/log/* 2>/dev/null | sort -h | tail -10        # log dolması
du -sh /var/lib/linux-ai-server/backups/* | sort -h       # eski backup
du -sh /opt/linux-ai-server/data/* | sort -h              # DB sismesi
docker system df -v                                        # docker image/volume

# Quick cleanup:
sudo journalctl --vacuum-size=100M
docker system prune -af --volumes  # ⚠️ DİKKAT — aktif olmayan her şey
```

**Senaryo: MEM yüksek (>88%)**
```bash
free -h
ps -eo pid,user,%mem,rss,cmd --sort=-%mem | head -10
# Memory leak şüphesi varsa:
sudo systemctl restart linux-ai-server
```

### B) "✗ Test runner FAILED" Telegram bildirimi
```bash
# Hangi proje fail etti?
tail -50 /opt/linux-ai-server/logs/test-runner-$(date +%Y%m%d).log

# Detay log:
ls -t /opt/linux-ai-server/logs/test-fail-*.log | head -3
tail -40 /opt/linux-ai-server/logs/test-fail-*-$(date +%Y%m%d)-*.log

# Coverage trend:
sqlite3 /opt/linux-ai-server/data/coverage.db \
  "SELECT timestamp, total_tests, total_failed FROM test_runs ORDER BY id DESC LIMIT 10;"
```

### C) Backup FAILED Telegram (daily-backup veya restore-test)
```bash
# daily-backup log
sudo tail -20 /var/log/linux-ai-server/backup.log

# Yaygın sebep: AUTH FAILED — API_KEY veya servis sorunu
# Test:
source /opt/linux-ai-server/.env
curl -s -X POST http://localhost:8420/api/v1/auth/token \
  -H 'Content-Type: application/json' -d "{\"api_key\":\"$API_KEY\"}"
# 200 dönmeli; 401 = API_KEY .env'de yanlış

# Restore-test fail:
sudo tail -30 /var/log/linux-ai-server/backup-restore-test.log
# DB bozuksa: kalıcı corruption, eski backup'ı seç manuel
ls -lt /var/lib/linux-ai-server/backups/
```

---

## 2. Servis fail senaryoları

### linux-ai-server.service down
```bash
systemctl status linux-ai-server
journalctl -u linux-ai-server -n 50 --no-pager
sudo systemctl restart linux-ai-server
# Eğer "active" görünüyor ama 502/connection refused:
curl -s http://localhost:8420/health
ss -tlnp | grep 8420
```

### n8n container down
```bash
docker ps -a --filter name=n8n
docker logs n8n --tail 50
cd /opt/n8n && sudo docker compose up -d
# Webhook secret değişimi gerekirse:
grep ^WEBHOOK_SECRET /opt/linux-ai-server/.env
```

### Docker stack genel
```bash
docker ps -a
docker system df
# Hangi container restart loop?
docker ps --filter "status=restarting"
```

### Cron çalışmıyor (heartbeat yok)
```bash
# Cron job son ne zaman çalıştı?
ls -lt /var/log/linux-ai-server/*.log | head

# klipper-cron-wrap event log
sudo tail -20 /opt/linux-ai-server/data/klipper-event.log
```

---

## 3. Backup operasyonları

### Manuel backup oluştur
```bash
source /opt/linux-ai-server/.env
TOKEN=$(curl -s -X POST http://localhost:8420/api/v1/auth/token \
  -H 'Content-Type: application/json' -d "{\"api_key\":\"$API_KEY\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8420/api/v1/backup/create?label=manual-$(date +%Y%m%d)"
```

### Restore (gerçek veri kaybı durumunda)
```bash
# 1) En yeni backup'tan listele
ls -lt /var/lib/linux-ai-server/backups/ | head -5

# 2) Geçici dizine aç
TMP=$(mktemp -d -t restore-XXXXXX)
tar -xzf /var/lib/linux-ai-server/backups/<seçilen>.tar.gz -C "$TMP"
ls "$TMP/data/"

# 3) ⚠️ Servis durdur, mevcut DB'yi yedekle, restore et
sudo systemctl stop linux-ai-server
cp /opt/linux-ai-server/data/server.db /opt/linux-ai-server/data/server.db.before-restore-$(date +%Y%m%d-%H%M)
cp "$TMP/data/server.db" /opt/linux-ai-server/data/server.db
sudo systemctl start linux-ai-server

# 4) Sağlık kontrol
curl -s http://localhost:8420/health
```

### Backup monitor manuel tetik
```bash
sudo systemctl start backup-monitor.service
sudo journalctl -u backup-monitor.service -n 20 --no-pager
```

---

## 4. Hafıza sistemi sorunları

### claude_memory.db kilitli / bozuk
```bash
sqlite3 /opt/linux-ai-server/data/claude_memory.db 'PRAGMA integrity_check;'
# "ok" değilse: en yeni backup'tan restore
```

### Memory API erişilemiyor
```bash
curl -s -H "X-Memory-Key: $(grep ^MEMORY_API_KEY /opt/linux-ai-server/.env | cut -d= -f2)" \
  http://localhost:8420/api/v1/memory/dashboard
```

---

## 5. Otonom Claude akışı problemleri

```bash
# klipper-auto user spawn'ları
ls -lt /opt/linux-ai-server/data/hook-logs/ | head -10

# Stale lock temizlik
find /opt/linux-ai-server/data/hook-state -mtime +1 -type f -delete

# Spawn DURDURMA (acil)
sudo systemctl stop autonomous-*.timer 2>/dev/null
# veya .env.autonomous'da AUTONOMOUS_ENABLED=false
```

---

## 6. Sık karşılaşılan referanslar

| Konu | Path / Komut |
|---|---|
| Cron job log'ları | `/var/log/linux-ai-server/<name>.log` |
| Service log | `journalctl -u linux-ai-server -f` |
| Test runner | `/opt/linux-ai-server/logs/test-runner-YYYYMMDD.log` |
| Backup'lar | `/var/lib/linux-ai-server/backups/` |
| Memory DB | `/opt/linux-ai-server/data/claude_memory.db` |
| State files | `/var/lib/linux-ai-server/alert-state/` |
| Hook logs | `/opt/linux-ai-server/data/hook-logs/` |
| .env (secrets) | `/opt/linux-ai-server/.env` (mode 0600) |
| systemd env | `/etc/systemd/system/linux-ai-server.service.d/vps-env.conf` |
| n8n compose | `/opt/n8n/docker-compose.yml` (→ `infra/n8n/`) |

## 7. Acil iletişim / bilinen sınırlar

- **Tek admin:** turgut.urer@gmail.com
- **Backup retention:** 7 gün lokal `/var/lib/linux-ai-server/backups/`. **Off-site backup yok** — SSD failure = total loss.
- **Klipper home server:** güç kesintisi → tüm servisler down. UPS yok.
- **Auto-healing yok:** Tüm alarm Telegram'a düşer, manuel müdahale beklenir. Self-Healing workflow alarm dağıtım rolünde.

## 8. Memory referansları (geçmiş incident'lar)

Sorunla karşılaşınca ilk olarak benzer geçmiş incident:

```bash
ls /home/klipperos/.claude/projects/-opt-linux-ai-server/memory/fix_*.md
sqlite3 /opt/linux-ai-server/data/claude_memory.db \
  "SELECT title FROM discoveries WHERE type='fix' AND created_at > date('now','-30 day') ORDER BY id DESC LIMIT 20;"
```
