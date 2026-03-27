"""FastAPI application factory and server entry point."""

from __future__ import annotations

import uvicorn
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
from app.exceptions import ServerError
from app.middleware.request_id import RequestIdMiddleware


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

    app.add_middleware(RequestIdMiddleware)

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
