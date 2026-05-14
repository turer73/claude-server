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

MSG='🔔 *Gozlem haftasi sonu — 1 hafta dolduran karar zamani*

2026-05-14 oturumunda eklenenler icin bugun (21 May) checklist:

1. Telegram /research bot — kac sorgu geldi?
   \`sqlite3 /opt/linux-ai-server/data/rag_metrics.db "SELECT COUNT(*) FROM rag_queries WHERE ts > strftime(%s,\"2026-05-14\")"\`

2. Cron 7 gun — alert/audit/health Telegrama geldi mi?
   \`ls -la /var/log/linux-ai-server/\` — her log dosyasinda satir birikmesi

3. /data/backups/vps/ — 7 snapshot dizini olmali (gunluk 04:00)

4. n8n smart-approve Pazar 11:00 calisti mi?
   \`sqlite3 .../n8n/database.sqlite "SELECT status,startedAt FROM execution_entity WHERE workflowId=panola_auto_approve ORDER BY id DESC LIMIT 3"\`

5. Petvet/Kuafor PR — merge edildi mi? Canli header verify?
   \`gh pr list --repo turer73/petvet\`

6. Stirling AutoPipeline — 0 error of 7 gun?

7. Self-pentest Pazar 03:30 — yeni bug?

KARAR:
- Yuksek kullanim → Faz 4/6 (web search, memory consolidator)
- Dusuk kullanim → feature dondur
- qwen kalitesiz → Claude haiku default

Detay: memory #555 (claude-memory.sh get 555)'

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
