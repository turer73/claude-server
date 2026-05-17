#!/bin/bash
# Bir-seferlik hatirlatma: 2026-05-14'te baslayan gozlem haftasinin sonu (21 May).
# Telegram'a checklist mesaji gonderir, sonra kendi crontab entry'sini siler.
# Memory ref: #555 (project: "Gozlem haftasi 2026-05-14 -> 2026-05-21")
set -euo pipefail

source /opt/linux-ai-server/.env 2>/dev/null

# Sadece 2026'da fire et (cron yearly tekrarli olmamali)
if [ "$(date +%Y)" != "2026" ]; then
    exit 0
fi

MSG='🔔 *Gozlem haftasi sonu — 21 May karar bloku (REVIZE 2026-05-17)*

⚠️ Bu mesaj 17 May Bilge Arena PII saldirisi sonrasi guncellendi.
Once asagidaki 17 May durumu oku, sonra orjinal checkliste gec.

═══ 17 MAY OLAY ZINCIRI (oku, karari etkiler) ═══

• 16 May 22:58 TR: Bilge Arena 191 profil PII dump bildirimi (Ensar/insider)
• 17 May 00-05 TR: Saldiri kapanisi (8 RPC anon REVOKE, 35 bot, 31 domain block)
• 17 May 12:49-12:54 TR: Madde 9 #5/#6 MERGED, #7 (PR #148) OPEN
• 17 May: Service-role JWT rotate baslatildi (surer-side, beklenen rapor)
• 17 May: KVKK avukat brief hazirlandi (tmp/kvkk-avukat-brief-2026-05-17.md)

Acik:
- KVKK 72 saat deadline: 19 May 22:58 TR (bildirim karari verildi mi?)
- Service-role rotate tamamlandi mi? (Vercel + VPS + n8n + .env.local)
- #148 BLOCKER fixleri (B1 use-auth flag, B2 cooldown, B3 UUID regex) merge oldu mu?
- Madde 9 #8/#9/#10 21 May SONRASI karari teyit (memory #568)

═══ 21 MAY KARAR KALEMLERI ═══

A) /research bot — orjinal eski karar *dondur* yonune kayiyor:
   17 May itibariyla 1 sorgu/7 gun. Eski matriste 0-2 = dondur.
   AMA: Bilge Arena ONCELIK modu (note #102) bu kararı override edebilir.
   Karar: Faz 4/6 vs dondurma vs Bilge LoRA odakli reorganizasyon.

B) Madde 9 #8/#9/#10 sprint — memory #568 erteleme karari:
   Bugun (21 May) gozden gecirilecek: baslat / ertelemeye devam / iptal.
   #10 final REVOKE sadece rotate dogrulandi ise yapilabilir.

C) Bilge Arena ONCELIK modu resmilestir:
   Memory #501 Ay 2 LoRA hedefi Bilge Arena icin guclendi (191 gercek kullanici).
   Karar: Stripe/iyzico premium odeme (#172) + #346 Realtime DB mismatch oncelikli mi?

═══ ORJINAL OPERASYONEL CHECKLIST ═══

1. Cron 7 gun — alert/audit/health Telegrama geldi mi?
   \`ls -la /var/log/linux-ai-server/\`

2. /data/backups/vps/ — 7 snapshot dizini olmali (gunluk 04:00)

3. n8n smart-approve Pazar 11:00 calisti mi?
   \`sqlite3 .../n8n/database.sqlite "SELECT status,startedAt FROM execution_entity WHERE workflowId=panola_auto_approve ORDER BY id DESC LIMIT 3"\`

4. Petvet/Kuafor PR — merge edildi (0 acik teyit) + canli header verify
   \`curl -sIL https://petvet.panola.app | grep -i strict-transport\`

5. Stirling AutoPipeline — 0 error of 7 gun?

6. Self-pentest Pazar 03:30 — yeni bug?

7. Vulkan Ollama stable mi (memory #560 madde 2)?

Detay: memory #555, #560, #568 + notes #102, #103, #104.
\`bash /opt/linux-ai-server/scripts/claude-memory.sh get 568\`'

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" \
    -d parse_mode="Markdown" \
    -d disable_web_page_preview="true" \
    --data-urlencode "text=${MSG}" >/dev/null

# Self-cleanup: bu cron entry'sini sil (yillik tekrari engelle)
crontab -l 2>/dev/null \
  | grep -v "observation-week-end-reminder.sh" \
  | crontab -

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] gozlem haftasi hatirlatmasi gonderildi, cron entry silindi"
