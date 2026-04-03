# Remote Memory Skill — Windows / Dış Cihazlar İçin

Bu dosyayı Claude Code kullanan Windows makinelerdeki `.claude/skills/memory/SKILL.md` olarak kaydet.

## Kurulum

Windows'ta PowerShell:
```powershell
mkdir -Force "$env:USERPROFILE\.claude\skills\memory"
# Sonra aşağıdaki SKILL.md içeriğini kaydet
```

## SKILL.md İçeriği

```markdown
---
name: memory
description: Merkezi hafıza sistemi — Klipper sunucuya bağlan, oturum kaydet, bug logla, ara
allowed-tools: Bash(curl *)
---

# Merkezi Hafıza Sistemi (Remote)

Klipper sunucusundaki SQLite hafıza veritabanına API üzerinden bağlan.

**API:** `http://100.113.153.62:8420/api/v1/memory`
**Key:** `fTczkso1377pSg_f7XxfFfn_R7AXkL-QTzkTihMRg2A`
**Header:** `X-Memory-Key: fTczkso1377pSg_f7XxfFfn_R7AXkL-QTzkTihMRg2A`
**Cihaz:** Bu cihazın adını `$DEVICE` olarak kullan (windows-masaustu veya windows-laptop)

## Komutlar

`/memory` → dashboard göster
`/memory save` → oturumu kaydet
`/memory bug <proje> <başlık>` → bug kaydet
`/memory fix <id>` → bug çöz
`/memory note <başlık> <içerik>` → not bırak
`/memory search <kelime>` → ara

## API Çağrıları

Dashboard:
curl -s -H "X-Memory-Key: KEY" http://100.113.153.62:8420/api/v1/memory/dashboard

Session kaydet:
curl -s -X POST http://100.113.153.62:8420/api/v1/memory/sessions -H "Content-Type: application/json" -H "X-Memory-Key: KEY" -d '{"device_name":"DEVICE","summary":"ÖZET"}'

Bug kaydet:
curl -s -X POST http://100.113.153.62:8420/api/v1/memory/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: KEY" -d '{"device_name":"DEVICE","project":"PROJE","type":"bug","title":"BAŞLIK","details":"DETAY"}'

Bug çöz:
curl -s -X PUT http://100.113.153.62:8420/api/v1/memory/discoveries/ID/resolve -H "X-Memory-Key: KEY"

Not:
curl -s -X POST http://100.113.153.62:8420/api/v1/memory/notes -H "Content-Type: application/json" -H "X-Memory-Key: KEY" -d '{"from_device":"DEVICE","title":"BAŞLIK","content":"İÇERİK"}'

Ara:
curl -s -H "X-Memory-Key: KEY" "http://100.113.153.62:8420/api/v1/memory/search?q=KELIME"

Okunmamış notlar:
curl -s -H "X-Memory-Key: KEY" "http://100.113.153.62:8420/api/v1/memory/notes?device=DEVICE&unread_only=true"
```
