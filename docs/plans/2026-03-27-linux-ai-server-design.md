# Linux-AI Server - Design Document

**Date:** 2026-03-27
**Author:** Zaman Huseyinli + Claude
**Status:** Approved

## Overview

Linux-AI Server is a production-grade API server that provides full kernel-level
Linux system control through both REST API and MCP (Model Context Protocol)
interfaces. It enables Claude and other clients to remotely manage, monitor,
develop on, and automate Linux servers.

## Goals

1. **Full kernel control** via REST API + MCP (governor, frequency, metrics, services)
2. **Internet access proxy** for Claude (HTTP requests, web API operations)
3. **Remote code development** (file CRUD, git, terminal, package management)
4. **Real-time monitoring** via WebSocket (CPU, RAM, disk, temp, network)
5. **Production-grade quality** (type safety, error handling, testing, CI/CD)
6. **Easy Linux deployment** (Docker, systemd, single binary install)

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    linux-ai-server                            │
│                                                              │
│  ┌─────────────┐  ┌───────────────┐  ┌───────────────────┐  │
│  │ MCP Server  │  │ REST API      │  │ WebSocket Server  │  │
│  │ (stdio/SSE) │  │ (FastAPI)     │  │ (terminal, live)  │  │
│  │ Claude ←────┤  │ :8420/api/v1  │  │ :8420/ws          │  │
│  └──────┬──────┘  └───────┬───────┘  └────────┬──────────┘  │
│         │                 │                    │              │
│         └─────────────────┼────────────────────┘              │
│                           ▼                                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                   Core Services                        │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ KernelBridge    │ ioctl /dev/ai_ctl, procfs, sysfs    │  │
│  │ SystemManager   │ processes, services (systemd)        │  │
│  │ FileManager     │ file CRUD, search, permissions       │  │
│  │ DevManager      │ git, scaffold, packages              │  │
│  │ NetworkProxy    │ HTTP/curl proxy for internet access  │  │
│  │ ShellExecutor   │ whitelisted command execution        │  │
│  │ TerminalManager │ WebSocket interactive terminal       │  │
│  │ MonitorAgent    │ real-time system metrics              │  │
│  │ LogManager      │ log aggregation, search, tail        │  │
│  │ WebOpsProxy     │ Vercel/CF/Supabase/GitHub/Coolify    │  │
│  │ AIInference     │ Ollama model calling                  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                   Infrastructure                       │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ Auth           │ API Key + JWT (1h TTL)                │  │
│  │ Database       │ SQLite (state, jobs, audit history)   │  │
│  │ Rate Limiter   │ Token bucket (100/min read, 10 write) │  │
│  │ Audit Logger   │ All write/exec ops → DB + file        │  │
│  │ Error Handler  │ Structured JSON errors + request ID   │  │
│  │ Health Check   │ /health, /ready endpoints             │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/auth/token` | Get JWT token with API key |
| GET | `/api/v1/auth/me` | Current user info |
| POST | `/api/v1/auth/refresh` | Refresh JWT token |

### Kernel Control
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/kernel/status` | Module status (state, governor, cpu_count) |
| GET | `/api/v1/kernel/governor` | Current governor mode |
| PUT | `/api/v1/kernel/governor` | Set governor (performance/powersave/ondemand/ai_adaptive) |
| GET | `/api/v1/kernel/cpu/{id}/metrics` | Per-core metrics (usage, freq, temp, IO) |
| GET | `/api/v1/kernel/cpu/frequency` | Frequency limits |
| PUT | `/api/v1/kernel/cpu/frequency` | Set min/max frequency |
| POST | `/api/v1/kernel/reset` | Reset module to defaults |
| GET | `/api/v1/kernel/permissions` | Check permission level |

### System Management
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/system/info` | CPU, RAM, disk, uptime, hostname |
| GET | `/api/v1/system/processes` | Process list (top N by CPU/RAM) |
| POST | `/api/v1/system/processes/{pid}/signal` | Send signal to process |
| GET | `/api/v1/system/services` | Systemd service list |
| POST | `/api/v1/system/services/{name}/{action}` | start/stop/restart/enable/disable |

### File Operations
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/files/read` | Read file content |
| PUT | `/api/v1/files/write` | Write/create file |
| PATCH | `/api/v1/files/edit` | Patch file (find/replace) |
| DELETE | `/api/v1/files/delete` | Delete file |
| GET | `/api/v1/files/list` | List directory |
| GET | `/api/v1/files/search` | Search files by name/content |
| GET | `/api/v1/files/info` | File metadata (size, perms, mtime) |

### Development
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/dev/scaffold` | Create project from template |
| POST | `/api/v1/dev/git/init` | Initialize git repo |
| GET | `/api/v1/dev/git/status` | Git status |
| POST | `/api/v1/dev/git/commit` | Git commit |
| POST | `/api/v1/dev/git/push` | Git push |
| GET | `/api/v1/dev/git/log` | Git log |
| GET | `/api/v1/dev/git/diff` | Git diff |
| POST | `/api/v1/dev/git/branch` | Create/switch branch |
| POST | `/api/v1/dev/packages/install` | Install packages (pip/npm/apt) |
| GET | `/api/v1/dev/packages/list` | List installed packages |

### Network / Internet Access
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/network/request` | HTTP proxy (GET/POST/PUT/DELETE) |
| GET | `/api/v1/network/interfaces` | Network interfaces |
| GET | `/api/v1/network/connections` | Active connections |
| POST | `/api/v1/network/dns` | DNS lookup |
| POST | `/api/v1/network/ping` | Ping host |

### Shell Execution
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/shell/exec` | Execute command (whitelist enforced) |
| POST | `/api/v1/shell/script` | Run multi-line script |
| WS | `/ws/terminal` | Interactive terminal (WebSocket + tmux) |

### Monitoring
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/monitor/metrics` | Current system metrics snapshot |
| GET | `/api/v1/monitor/history` | Historical metrics from DB |
| GET | `/api/v1/monitor/alerts` | Alert history |
| POST | `/api/v1/monitor/alerts/config` | Configure alert thresholds |
| WS | `/ws/monitor` | Real-time metrics stream |

### Logs
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/logs/search` | Search logs (regex, time range) |
| GET | `/api/v1/logs/tail` | Last N lines |
| GET | `/api/v1/logs/stats` | Log statistics |
| GET | `/api/v1/logs/sources` | Available log sources |
| WS | `/ws/logs` | Live log streaming |

### Web Operations
| Method | Endpoint | Description |
|--------|----------|-------------|
| * | `/api/v1/webops/vercel/*` | Vercel API proxy |
| * | `/api/v1/webops/cloudflare/*` | Cloudflare API proxy |
| * | `/api/v1/webops/supabase/*` | Supabase API proxy |
| * | `/api/v1/webops/github/*` | GitHub API proxy |
| * | `/api/v1/webops/coolify/*` | Coolify API proxy |

### AI Inference
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/ai/chat` | Chat with Ollama model |
| POST | `/api/v1/ai/tools/call` | Execute AI tool |
| GET | `/api/v1/ai/models` | Available models |

### Health
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/ready` | Readiness check |
| GET | `/api/v1/version` | Server version |

## MCP Tools

All core services are exposed as MCP tools for Claude integration:

```
kernel_status, kernel_set_governor, kernel_get_metrics,
kernel_set_frequency, kernel_reset

system_info, process_list, process_signal,
service_list, service_control

file_read, file_write, file_edit, file_delete,
file_list, file_search

dev_git_status, dev_git_commit, dev_git_push,
dev_git_log, dev_git_diff, dev_git_branch,
dev_scaffold, dev_package_install

http_request, dns_lookup, ping

shell_exec, shell_script

monitor_metrics, monitor_history, monitor_alerts

log_search, log_tail, log_stats

webops_vercel, webops_cloudflare, webops_supabase,
webops_github, webops_coolify

ai_chat, ai_tool_call
```

## Database Schema (SQLite)

```sql
-- Audit log for all write/exec operations
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    request_id TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    details TEXT,  -- JSON
    status TEXT NOT NULL,  -- success/error
    ip_address TEXT
);

-- Metrics history for monitoring
CREATE TABLE metrics_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_usage REAL,
    memory_usage REAL,
    disk_usage REAL,
    temperature REAL,
    load_avg TEXT,  -- JSON array
    network_io TEXT  -- JSON
);

-- Alert history
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    severity TEXT NOT NULL,  -- info/warning/critical
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT
);

-- API keys
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT 'read',  -- read/write/admin
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    active INTEGER DEFAULT 1
);

-- Job queue for background tasks
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,  -- JSON
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    result TEXT,  -- JSON
    error TEXT
);
```

## Security

### Authentication
- API Key in `X-API-Key` header
- JWT token (1 hour TTL, refresh supported)
- Permission levels: read, write, admin
- API keys stored as SHA-256 hashes in SQLite

### Authorization
- Read ops: `read` permission
- Write/exec ops: `write` permission
- Kernel control, service management: `admin` permission

### Rate Limiting
- Read operations: 100 requests/minute
- Write operations: 10 requests/minute
- Shell execution: 5 requests/minute
- Configurable per API key

### Input Validation
- All inputs validated with Pydantic v2 strict models
- Path traversal prevention (resolve + check prefix)
- Shell command whitelist enforcement
- File size limits (10 MB read, 5 MB write)
- Request body size limit (10 MB)

### Audit Logging
- Every write/exec/delete operation logged to DB
- Request ID tracking (UUID per request)
- IP address, user, timestamp, action, result

## WebSocket Terminal

Interactive terminal via WebSocket with tmux backend:

```
Client ←→ WebSocket ←→ TerminalManager ←→ tmux session
                                              │
                                    ┌─────────┤
                                    │  bash   │
                                    │  or     │
                                    │  custom │
                                    │  shell  │
                                    └─────────┘
```

- Each WebSocket connection gets a tmux session
- Session persistence (reconnect to existing session)
- Terminal resize support
- Idle timeout (30 minutes)
- Max concurrent sessions: 5

## Testing Strategy

### Unit Tests (pytest)
- Every core service module has dedicated test file
- Mock kernel bridge for non-Linux environments
- Pydantic model validation tests
- Auth/JWT tests with known tokens
- Target: 80%+ code coverage

### Integration Tests
- TestClient (httpx) against FastAPI app
- Full request/response cycle per endpoint
- Auth flow tests (API key → JWT → protected endpoint)
- WebSocket terminal tests
- Database migration tests

### End-to-End Tests
- Docker container startup test
- Health/ready endpoint checks
- Full CRUD cycle tests (file create → read → edit → delete)
- Git workflow test (init → commit → status)

### Load Tests (locust)
- Concurrent request handling
- WebSocket connection limits
- Rate limiter correctness
- Memory leak detection under load

### Security Tests
- Path traversal attempts
- SQL injection on search endpoints
- JWT tampering/expiration
- Rate limit bypass attempts
- Shell injection attempts

### CI/CD Pipeline
```yaml
# .github/workflows/ci.yml
jobs:
  lint:    ruff check + ruff format --check
  types:   mypy --strict
  test:    pytest --cov=app --cov-fail-under=80
  security: bandit -r app/
  build:   docker build + smoke test
```

## Project Structure

```
linux-ai-server/
├── pyproject.toml              # deps, ruff, mypy, pytest config
├── Dockerfile                  # multi-stage build
├── docker-compose.yml          # server + optional redis
├── Makefile                    # dev, test, lint, build, deploy
├── .github/workflows/ci.yml
├── config/
│   └── server.yml              # all server configuration
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory + lifespan
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py           # mount all routers
│   │   ├── kernel.py           # /api/v1/kernel/*
│   │   ├── system.py           # /api/v1/system/*
│   │   ├── files.py            # /api/v1/files/*
│   │   ├── dev.py              # /api/v1/dev/*
│   │   ├── network.py          # /api/v1/network/*
│   │   ├── shell.py            # /api/v1/shell/*
│   │   ├── monitoring.py       # /api/v1/monitor/*
│   │   ├── logs.py             # /api/v1/logs/*
│   │   ├── webops.py           # /api/v1/webops/*
│   │   ├── ai.py               # /api/v1/ai/*
│   │   └── auth.py             # /api/v1/auth/*
│   ├── ws/
│   │   ├── __init__.py
│   │   ├── terminal.py         # WebSocket terminal handler
│   │   ├── monitor.py          # WebSocket live metrics
│   │   └── logs.py             # WebSocket log streaming
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── server.py           # MCP server (stdio + SSE)
│   │   └── tools.py            # MCP tool definitions
│   ├── core/
│   │   ├── __init__.py
│   │   ├── kernel_bridge.py    # ioctl bridge to /dev/ai_ctl
│   │   ├── system_manager.py   # psutil + systemd
│   │   ├── file_manager.py     # safe file operations
│   │   ├── dev_manager.py      # git + scaffold + packages
│   │   ├── network_proxy.py    # httpx-based HTTP proxy
│   │   ├── shell_executor.py   # whitelisted shell execution
│   │   ├── terminal_manager.py # tmux WebSocket terminal
│   │   ├── monitor_agent.py    # metrics collection
│   │   ├── log_manager.py      # log aggregation
│   │   ├── webops_proxy.py     # web service API proxy
│   │   └── ai_inference.py     # Ollama integration
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── jwt_handler.py      # JWT create/verify
│   │   ├── api_key.py          # API key validation
│   │   └── permissions.py      # RBAC permission checks
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLite connection + migrations
│   │   ├── models.py           # SQLAlchemy/raw models
│   │   └── migrations/         # Schema versions
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py          # Pydantic v2 request/response
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── error_handler.py    # Global exception → JSON
│   │   ├── request_id.py       # UUID per request
│   │   ├── rate_limit.py       # Token bucket rate limiter
│   │   └── audit_log.py        # Write op audit logging
│   └── exceptions.py           # Custom exception hierarchy
├── tests/
│   ├── conftest.py             # Fixtures, test DB, mock bridge
│   ├── test_auth.py
│   ├── test_kernel.py
│   ├── test_system.py
│   ├── test_files.py
│   ├── test_dev.py
│   ├── test_network.py
│   ├── test_shell.py
│   ├── test_monitoring.py
│   ├── test_logs.py
│   ├── test_webops.py
│   ├── test_ai.py
│   ├── test_websocket.py
│   ├── test_mcp.py
│   ├── test_middleware.py
│   ├── test_db.py
│   └── test_security.py
└── scripts/
    ├── install.sh              # System install (systemd)
    ├── generate_api_key.py     # Create API keys
    └── migrate_db.py           # Run DB migrations
```

## Configuration (server.yml)

```yaml
server:
  host: 0.0.0.0
  port: 8420
  workers: 2
  debug: false

auth:
  jwt_secret: "${JWT_SECRET}"
  jwt_ttl_hours: 1
  api_key_header: "X-API-Key"

database:
  path: /var/lib/linux-ai-server/server.db
  metrics_retention_days: 30

rate_limit:
  read: 100    # per minute
  write: 10
  exec: 5

security:
  allowed_paths:
    - /var/AI-stump/
    - /proc/ai_*
    - /sys/ai/
    - /home/
    - /tmp/linux-ai-server/
  shell_whitelist:
    - ls, cat, head, tail, wc, grep, find
    - ps, top, df, free, uptime, whoami
    - systemctl status, journalctl
    - git, pip, npm, python3, node
    - make, cmake, gcc, g++
    - curl, wget, dig, nslookup
  max_file_size_mb: 10
  max_terminal_sessions: 5
  terminal_idle_timeout_min: 30

monitoring:
  poll_interval_sec: 5
  metrics_history_size: 1000
  alert_thresholds:
    cpu_percent: 85
    memory_percent: 85
    disk_percent: 90
    temperature_c: 80

webops:
  vercel_token: "${VERCEL_TOKEN}"
  cloudflare_token: "${CLOUDFLARE_TOKEN}"
  supabase_token: "${SUPABASE_TOKEN}"
  github_token: "${GITHUB_TOKEN}"
  coolify_token: "${COOLIFY_TOKEN}"
  coolify_url: "${COOLIFY_URL}"

logging:
  level: INFO
  format: json
  file: /var/log/linux-ai-server/server.log
  max_size_mb: 50
  backup_count: 5
```

## Deployment

### Docker
```bash
docker build -t linux-ai-server .
docker run -d \
  --name linux-ai-server \
  -p 8420:8420 \
  -v /var/AI-stump:/var/AI-stump \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  --device /dev/ai_ctl \
  --privileged \
  -e JWT_SECRET=xxx \
  linux-ai-server
```

### Systemd
```bash
sudo ./scripts/install.sh
sudo systemctl enable linux-ai-server
sudo systemctl start linux-ai-server
```

## Dependencies

### Required
- Python 3.11+
- fastapi, uvicorn[standard]
- pydantic>=2.0
- python-jose[cryptography] (JWT)
- httpx (HTTP proxy)
- psutil (system metrics)
- aiosqlite (async SQLite)
- pyyaml (config)

### Optional
- tmux (interactive terminal)
- ollama (AI inference)

## Hardware Requirements
- CPU: 1+ cores
- RAM: 512 MB minimum (server only)
- Disk: 100 MB + database growth
- OS: Linux (kernel 5.4+, systemd)
- Kernel module: linux_ai.ko (optional, graceful degradation)
