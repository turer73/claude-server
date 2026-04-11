#!/bin/bash
# deploy-n8n-workflow.sh — n8n'e workflow import et ve aktif et
# Kullanim: bash deploy-n8n-workflow.sh
set -euo pipefail

# ── Yapilandirma ──────────────────────────────────────
N8N_URL="${N8N_URL:-http://localhost:5678}"
N8N_API_KEY="${N8N_API_KEY:-}"
WORKFLOW_FILE="$(dirname "$0")/n8n-workflows/system-auto-repair.json"

# Renk
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "======================================"
echo " n8n Workflow Deploy — System Auto-Repair"
echo "======================================"

# ── 1. Onkosullar ────────────────────────────────────
if [ ! -f "$WORKFLOW_FILE" ]; then
    echo -e "${RED}HATA: Workflow dosyasi bulunamadi: $WORKFLOW_FILE${NC}"
    exit 1
fi

# n8n API key kontrolu
if [ -z "$N8N_API_KEY" ]; then
    echo ""
    echo -e "${YELLOW}n8n API key gerekli.${NC}"
    echo "n8n'de Settings > API > API Key olusturun."
    echo ""
    read -p "n8n API Key: " N8N_API_KEY
    if [ -z "$N8N_API_KEY" ]; then
        echo -e "${RED}HATA: API key bos olamaz${NC}"
        exit 1
    fi
fi

# ── 2. n8n Erisim Kontrolu ──────────────────────────
echo ""
echo "[1/5] n8n erisim kontrolu..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    "$N8N_URL/api/v1/workflows" 2>/dev/null)

if [ "$HTTP_CODE" = "000" ]; then
    echo -e "${RED}HATA: n8n'e erisilemedi ($N8N_URL)${NC}"
    echo "n8n calistigindan ve URL'in dogru oldugundan emin olun."
    exit 1
elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    echo -e "${RED}HATA: API key gecersiz (HTTP $HTTP_CODE)${NC}"
    echo "n8n Settings > API > API Key kontrol edin."
    exit 1
elif [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}HATA: Beklenmeyen yanit (HTTP $HTTP_CODE)${NC}"
    exit 1
fi
echo -e "${GREEN}  n8n erisilebilir ✔${NC}"

# ── 3. Mevcut Workflow Kontrolu ──────────────────────
echo "[2/5] Mevcut workflow kontrolu..."
EXISTING=$(curl -s --max-time 10 \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    "$N8N_URL/api/v1/workflows" 2>/dev/null)

EXISTING_ID=$(echo "$EXISTING" | python3 -c "
import sys, json
data = json.load(sys.stdin)
workflows = data.get('data', data) if isinstance(data, dict) else data
if isinstance(workflows, list):
    for wf in workflows:
        if 'Auto-Repair' in wf.get('name', ''):
            print(wf['id'])
            break
" 2>/dev/null || echo "")

if [ -n "$EXISTING_ID" ]; then
    echo -e "${YELLOW}  Mevcut workflow bulundu (ID: $EXISTING_ID) — guncellenecek${NC}"
fi

# ── 4. Workflow Import ───────────────────────────────
echo "[3/5] Workflow import ediliyor..."

# Workflow JSON'dan sadece gerekli alanlari al (id/versionId kaldir)
CLEAN_WORKFLOW=$(python3 -c "
import json, sys
with open('$WORKFLOW_FILE') as f:
    wf = json.load(f)
# Import icin gereksiz alanlari kaldir
for key in ['id', 'versionId']:
    wf.pop(key, None)
print(json.dumps(wf))
" 2>/dev/null)

if [ -z "$CLEAN_WORKFLOW" ]; then
    echo -e "${RED}HATA: Workflow JSON okunamadi${NC}"
    exit 1
fi

if [ -n "$EXISTING_ID" ]; then
    # Guncelle
    RESULT=$(curl -s --max-time 30 \
        -X PUT \
        -H "X-N8N-API-KEY: $N8N_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$CLEAN_WORKFLOW" \
        "$N8N_URL/api/v1/workflows/$EXISTING_ID" 2>/dev/null)
    WORKFLOW_ID="$EXISTING_ID"
else
    # Yeni olustur
    RESULT=$(curl -s --max-time 30 \
        -X POST \
        -H "X-N8N-API-KEY: $N8N_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$CLEAN_WORKFLOW" \
        "$N8N_URL/api/v1/workflows" 2>/dev/null)
    WORKFLOW_ID=$(echo "$RESULT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('id', ''))" 2>/dev/null || echo "")
fi

if [ -z "$WORKFLOW_ID" ]; then
    echo -e "${RED}HATA: Workflow import basarisiz${NC}"
    echo "Yanit: $RESULT"
    exit 1
fi
echo -e "${GREEN}  Workflow import edildi (ID: $WORKFLOW_ID) ✔${NC}"

# ── 5. Workflow Aktif Et ─────────────────────────────
echo "[4/5] Workflow aktif ediliyor..."
ACTIVATE=$(curl -s --max-time 10 \
    -X PATCH \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"active": true}' \
    "$N8N_URL/api/v1/workflows/$WORKFLOW_ID" 2>/dev/null)

IS_ACTIVE=$(echo "$ACTIVATE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('active', False))" 2>/dev/null || echo "False")

if [ "$IS_ACTIVE" = "True" ]; then
    echo -e "${GREEN}  Workflow aktif ✔${NC}"
else
    echo -e "${YELLOW}  Workflow import edildi ama aktif edilemedi — n8n'den manuel aktif edin${NC}"
fi

# ── 6. Dogrulama ─────────────────────────────────────
echo "[5/5] Dogrulama..."
VERIFY=$(curl -s --max-time 10 \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    "$N8N_URL/api/v1/workflows/$WORKFLOW_ID" 2>/dev/null)

VERIFY_NAME=$(echo "$VERIFY" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('name', 'BILINMIYOR'))" 2>/dev/null || echo "BILINMIYOR")

VERIFY_ACTIVE=$(echo "$VERIFY" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('active', False))" 2>/dev/null || echo "False")

VERIFY_NODES=$(echo "$VERIFY" | python3 -c "
import sys, json
data = json.load(sys.stdin)
nodes = data.get('nodes', [])
print(len([n for n in nodes if n.get('type', '') != 'n8n-nodes-base.stickyNote']))" 2>/dev/null || echo "0")

echo ""
echo "======================================"
echo -e " Sonuc:"
echo -e "   Ad:     $VERIFY_NAME"
echo -e "   ID:     $WORKFLOW_ID"
echo -e "   Aktif:  $VERIFY_ACTIVE"
echo -e "   Node:   $VERIFY_NODES"
echo -e "   URL:    $N8N_URL/workflow/$WORKFLOW_ID"
echo "======================================"

if [ "$VERIFY_ACTIVE" = "True" ] && [ "$VERIFY_NODES" -gt 5 ]; then
    echo ""
    echo -e "${GREEN}Deploy basarili. Workflow aktif ve webhook dinliyor:${NC}"
    echo -e "${GREEN}  POST $N8N_URL/webhook/system-alert${NC}"
    echo ""
    echo "Gerekli env vars:"
    echo "  LINUX_AI_API_KEY — linux-ai-server API key"
    echo "  TELEGRAM_BOT_TOKEN — Telegram bot token"
    echo "  TELEGRAM_CHAT_ID — Telegram chat ID"
else
    echo ""
    echo -e "${YELLOW}Workflow import edildi ama dogrulama eksik.${NC}"
    echo "n8n panelinden kontrol edin: $N8N_URL/workflow/$WORKFLOW_ID"
fi
