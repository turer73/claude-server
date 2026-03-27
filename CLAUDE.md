# Linux-AI Server

## Overview
Production-grade API server for full kernel-level Linux control via REST API and MCP.
Enables Claude and other clients to remotely manage, monitor, develop on, and automate Linux servers.

## Quick Start
```bash
pip install -e ".[dev]"    # Install with dev deps
make test                  # Run 244+ tests
make dev                   # Start dev server on :8420
```

## Project Structure
- `app/api/` — FastAPI route handlers
- `app/core/` — Core business logic
- `app/auth/` — JWT + API key authentication
- `app/db/` — SQLite database
- `app/middleware/` — Request ID, rate limiting, audit
- `app/models/` — Pydantic schemas
- `app/mcp/` — MCP server for Claude integration
- `app/ws/` — WebSocket handlers
- `tests/` — pytest test suite
- `scripts/` — Install, migrate, generate API key
- `config/` — YAML configuration

## API Base URL
`http://localhost:8420/api/v1/`

## Key Endpoints
- `/health` — Health check
- `/docs` — Swagger UI
- `/api/v1/auth/token` — Get JWT token
- `/api/v1/kernel/*` — Kernel control
- `/api/v1/system/*` — System management
- `/api/v1/files/*` — File operations
- `/api/v1/shell/*` — Shell execution
- `/api/v1/ssh/*` — SSH client
- `/api/v1/agents/*` — Agent management
- `/api/v1/monitor/*` — Monitoring
- `/api/v1/logs/*` — Log management
- `/api/v1/network/*` — Network proxy
- `/api/v1/dev/*` — Development tools
- `/api/v1/webops/*` — Web service proxy
- `/api/v1/ai/*` — AI inference
- `/ws/monitor` — Live metrics
- `/ws/terminal` — Interactive terminal
- `/ws/logs` — Live log stream

## Testing
```bash
make test        # Full suite with coverage
make test-fast   # Quick run, stop on first failure
make lint        # Ruff linter
make check       # lint + type-check + security + test
```
