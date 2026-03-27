"""FastAPI dependencies for auth, rate limiting, and audit logging."""

from __future__ import annotations

from fastapi import Request, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.jwt_handler import decode_token
from app.core.config import Settings, get_settings
from app.middleware.rate_limit import TokenBucketLimiter
from app.middleware.audit_log import AuditLogger
from app.middleware.request_id import request_id_var
from app.exceptions import RateLimitError, AuthenticationError, AuthorizationError

# ---------- Auth ----------
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Verify JWT token and return decoded claims.

    Every protected route MUST depend on this.
    Returns: {"sub": "key-name", "permissions": "admin|read|..."}
    """
    if credentials is None:
        raise AuthenticationError("Bearer token required")
    payload = decode_token(credentials.credentials, settings.jwt_secret)
    request.state.user = payload.get("sub", "unknown")
    request.state.permissions = payload.get("permissions", "")
    return payload


async def require_admin(claims: dict = Depends(require_auth)) -> dict:
    """Require admin permissions."""
    if claims.get("permissions") != "admin":
        raise AuthorizationError("Admin access required")
    return claims


async def require_write(claims: dict = Depends(require_auth)) -> dict:
    """Require write or admin permissions."""
    perms = claims.get("permissions", "")
    if perms not in ("admin", "write"):
        raise AuthorizationError("Write access required")
    return claims


# ---------- Rate Limiting ----------
_read_limiter = TokenBucketLimiter(rate=100, per_seconds=60)
_write_limiter = TokenBucketLimiter(rate=10, per_seconds=60)
_exec_limiter = TokenBucketLimiter(rate=5, per_seconds=60)
_global_limiter = TokenBucketLimiter(rate=200, per_seconds=60)


def _get_client_key(request: Request) -> str:
    """Extract client identifier for rate limiting."""
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return api_key[:16]
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


async def rate_limit_global(request: Request) -> None:
    """Global rate limit fallback (200/min per client)."""
    key = _get_client_key(request)
    if not _global_limiter.allow(key):
        raise RateLimitError("Global rate limit exceeded (200/min)")


# ---------- Audit ----------
async def audit_write(request: Request) -> None:
    """Log write operations to audit log (if DB available)."""
    request.state.audit_action = f"{request.method} {request.url.path}"
