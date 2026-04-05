"""FastAPI application factory and server entry point."""

from __future__ import annotations

import uvicorn
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer

from app import __version__
from app.api.auth import router as auth_router
from app.api.kernel import router as kernel_router
from app.api.system import router as system_router
from app.api.files import router as files_router
from app.api.shell import router as shell_router
from app.api.network import router as network_router
from app.api.dev import router as dev_router
from app.api.ssh import router as ssh_router
from app.api.agents import router as agents_router
from app.api.webops import router as webops_router
from app.api.ai import router as ai_router
from app.api.monitoring import router as monitoring_router
from app.api.logs import router as logs_router
from app.ws.monitor import router as ws_monitor_router
from app.ws.terminal import router as ws_terminal_router
from app.ws.logs import router as ws_logs_router
from app.api.prometheus import router as prometheus_router
from app.api.backup import router as backup_router
from app.api.rag import router as rag_router
from app.api.devops import router as devops_router
from app.api.deploy import router as deploy_router
from app.api.vps import router as vps_router
from app.api.tasks import router as tasks_router
from app.api.ws_status import router as ws_status_router
from app.api.claude_code import router as claude_code_router
from app.api.projects import router as projects_router
from app.api.social import router as social_router
from app.api.memory import router as memory_router, public_router as memory_public_router
from app.api.validation import router as validation_router
from app.exceptions import ServerError
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.audit_log import AuditMiddleware
from app.middleware.rate_limit import GlobalRateLimitMiddleware

security_scheme = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import os
    from app.db.database import Database
    from app.auth.api_key import hash_api_key, generate_api_key

    db_path = os.environ.get("DB_PATH", "/tmp/linux-ai-server-test.db")
    db = Database(db_path)
    await db.initialize()

    # Create default admin key if no keys exist
    existing = await db.fetch_all("SELECT id FROM api_keys LIMIT 1")
    if not existing:
        default_key = os.environ.get("DEFAULT_API_KEY", generate_api_key())
        await db.execute(
            "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
            (hash_api_key(default_key), "admin", "admin"),
        )

    app.state.db = db

    # Start DevOps Agent daemon
    from app.core.devops_agent import DevOpsAgent
    devops = DevOpsAgent(db=db, interval=30)
    app.state.devops_agent = devops
    devops.start()

    # Start Task Queue worker
    from app.core.task_queue import TaskQueue
    task_queue = TaskQueue(db=db)
    app.state.task_queue = task_queue
    task_queue.start()

    yield

    # Graceful shutdown
    await task_queue.stop()
    await devops.stop()
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Linux-AI Server",
        description="Full kernel-level Linux control via REST API and MCP",
        version=__version__,
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # Middleware order matters: outermost first
    # 1. Request ID — adds x-request-id to every request
    app.add_middleware(RequestIdMiddleware)

    # 2. CORS — browser cross-origin support
    from app.core.config import get_settings
    _settings = get_settings()
    _cors_origins = [
        "http://localhost:8420",
        "http://localhost:3000",
        "http://REDACTED_LAN_IP:8420",
        "http://REDACTED_TAILSCALE_IP:8420",
        "https://panola.app",
        "https://petvet.panola.app",
        "https://kuafor.panola.app",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3. Audit — logs all POST/PUT/PATCH/DELETE to DB
    app.add_middleware(AuditMiddleware)

    # 4. Global rate limit — 200 req/min per client IP (safety net)
    app.add_middleware(GlobalRateLimitMiddleware, rate=200, per_seconds=60)

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

    # ---- Routes ----
    app.include_router(auth_router)
    app.include_router(kernel_router)
    app.include_router(system_router)
    app.include_router(files_router)
    app.include_router(shell_router)
    app.include_router(network_router)
    app.include_router(dev_router)
    app.include_router(ssh_router)
    app.include_router(agents_router)
    app.include_router(webops_router)
    app.include_router(ai_router)
    app.include_router(monitoring_router)
    app.include_router(logs_router)
    app.include_router(ws_monitor_router)
    app.include_router(ws_terminal_router)
    app.include_router(ws_logs_router)
    app.include_router(prometheus_router)
    app.include_router(backup_router)
    app.include_router(ws_status_router)
    app.include_router(rag_router)
    app.include_router(devops_router)
    app.include_router(deploy_router)
    app.include_router(vps_router)
    app.include_router(tasks_router)
    app.include_router(claude_code_router)
    app.include_router(projects_router)
    app.include_router(social_router)
    app.include_router(memory_router)
    app.include_router(memory_public_router)
    app.include_router(validation_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict:
        return {"ready": True, "version": __version__}

    # Dashboard — serve at /dashboard
    dashboard_dir = Path(__file__).parent / "dashboard"
    if dashboard_dir.is_dir():
        @app.get("/dashboard")
        async def dashboard():
            return FileResponse(dashboard_dir / "index.html")

    # Claude Code UI — serve at /claude
    claude_ui_dir = Path(__file__).parent / "claude_ui"
    if claude_ui_dir.is_dir():
        @app.get("/claude")
        async def claude_page():
            return FileResponse(claude_ui_dir / "index.html")

    return app


def main() -> None:
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8420, workers=2)


if __name__ == "__main__":
    main()
