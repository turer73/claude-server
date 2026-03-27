"""Token bucket rate limiter — in-memory, per-key + global ASGI middleware."""

from __future__ import annotations

import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


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
        if self.rate <= 0:
            return False

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


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware — global rate limit safety net for ALL routes."""

    SKIP_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}

    def __init__(self, app, rate: int = 200, per_seconds: int = 60) -> None:
        super().__init__(app)
        self._limiter = TokenBucketLimiter(rate=rate, per_seconds=per_seconds)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        # Extract client IP
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "unknown"

        if not self._limiter.allow(client_ip):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RateLimitError",
                    "message": "Global rate limit exceeded (200/min)",
                    "detail": None,
                },
            )

        return await call_next(request)
