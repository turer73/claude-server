"""Token bucket rate limiter — in-memory, per-key."""

from __future__ import annotations

import time
from dataclasses import dataclass


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
