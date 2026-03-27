"""FastAPI dependencies for rate limiting and audit logging."""

from __future__ import annotations

from fastapi import Request

from app.middleware.rate_limit import TokenBucketLimiter
from app.middleware.audit_log import AuditLogger
from app.middleware.request_id import request_id_var
from app.exceptions import RateLimitError

# Singleton rate limiters for different tiers
_read_limiter = TokenBucketLimiter(rate=100, per_seconds=60)
_write_limiter = TokenBucketLimiter(rate=10, per_seconds=60)
_exec_limiter = TokenBucketLimiter(rate=5, per_seconds=60)


def _get_client_key(request: Request) -> str:
    """Extract client identifier for rate limiting."""
    # Use API key from header, or IP address
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return api_key[:16]  # Use prefix of key
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_read(request: Request) -> None:
    """Rate limit for read operations (100/min)."""
    key = _get_client_key(request)
    if not _read_limiter.allow(key):
        raise RateLimitError("Read rate limit exceeded (100/min)")


async def rate_limit_write(request: Request) -> None:
    """Rate limit for write operations (10/min)."""
    key = _get_client_key(request)
    if not _write_limiter.allow(key):
        raise RateLimitError("Write rate limit exceeded (10/min)")


async def rate_limit_exec(request: Request) -> None:
    """Rate limit for exec operations (5/min)."""
    key = _get_client_key(request)
    if not _exec_limiter.allow(key):
        raise RateLimitError("Exec rate limit exceeded (5/min)")


async def audit_write(request: Request) -> None:
    """Log write operations to audit log (if DB available)."""
    # Store intent on request state for post-processing
    request.state.audit_action = f"{request.method} {request.url.path}"
