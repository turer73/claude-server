"""Audit logger — ASGI middleware + manual logger for write/exec operations."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db.database import Database
from app.middleware.request_id import request_id_var


class AuditLogger:
    """Direct audit log writer for manual use."""

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


class AuditMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that automatically logs all mutating requests."""

    # Only audit mutating methods
    AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    # Skip paths that don't need audit
    SKIP_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in self.AUDITED_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in self.SKIP_PATHS:
            return await call_next(request)

        start = time.time()
        response = await call_next(request)
        elapsed = time.time() - start

        # Try to log — fail silently if DB not available
        try:
            db: Database | None = getattr(request.app.state, "db", None)
            if db:
                req_id = request_id_var.get("no-id")
                user = getattr(request.state, "user", "anonymous")
                status = "success" if response.status_code < 400 else "error"
                logger = AuditLogger(db)
                await logger.log(
                    request_id=req_id,
                    user=user,
                    action=f"{request.method} {path}",
                    resource=path,
                    status=status,
                    details=f"status={response.status_code} elapsed={elapsed:.3f}s",
                    ip_address=request.client.host if request.client else None,
                )
        except Exception:
            pass  # Audit should never break the request

        return response
