# n8n Workflow Examples for Linux-AI Server

## Setup

1. Start the full stack:
   ```bash
   docker-compose -f docker-compose.n8n.yml up -d
   ```

2. Open n8n: http://localhost:5678
3. Import workflows from this directory

## Available Workflows

### 1. System Health Monitor (every 5 min)
- Schedule trigger -> HTTP Request (GET /api/v1/monitor/metrics)
- IF cpu > 85% -> Telegram/Discord alert
- Store metrics in Supabase/Google Sheets

### 2. Backup Automation (daily at 2 AM)
- Cron trigger (0 2 * * *)
- HTTP Request (POST /api/v1/monitor/webhooks/trigger/backup_create)
- IF success -> log to channel
- IF failure -> alert admin

### 3. Deploy Pipeline
- GitHub webhook -> n8n webhook
- HTTP Request (POST /api/v1/shell/exec {"command": "git pull"})
- HTTP Request (POST /api/v1/shell/exec {"command": "make build"})
- Health check -> notify

### 4. Alert Escalation
- Schedule (every 1 min) -> POST /api/v1/monitor/webhooks/trigger/alert_check
- IF has_alerts -> Telegram
- IF critical -> Telegram + Discord + Email

## API Connection in n8n

Use "HTTP Request" node with:
- URL: http://linux-ai-server:8420/api/v1/...
- Authentication: Header Auth
  - Name: X-API-Key
  - Value: (your API key)

Or get JWT first:
1. POST http://linux-ai-server:8420/api/v1/auth/token
   Body: {"api_key": "your-key"}
2. Use returned access_token as Bearer token
