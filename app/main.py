"""FastAPI application factory and server entry point."""

from __future__ import annotations

import uvicorn
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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
from app.api.ws_status import router as ws_status_router
from app.exceptions import ServerError
from app.middleware.request_id import RequestIdMiddleware

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
    yield
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Linux-AI Server",
        description="Full kernel-level Linux control via REST API and MCP",
        version=__version__,
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    app.add_middleware(RequestIdMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
