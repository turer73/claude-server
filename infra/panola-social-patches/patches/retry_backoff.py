"""Anthropic 429/529 exponential backoff decorator.

Deploy: /opt/panola-social/src/utils/retry_backoff.py
Usage:
    from src.utils.retry_backoff import anthropic_retry

    @anthropic_retry()
    def call_claude(prompt: str) -> str:
        ...
"""
from __future__ import annotations

import logging
import random
import time
from functools import wraps
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

_RETRYABLE_CODES = {429, 529}


def anthropic_retry(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: float = 0.5,
) -> Callable[[F], F]:
    """Decorator: Anthropic 429 (rate-limit) ve 529 (overloaded) için exp backoff."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    status = _extract_status(exc)
                    if status not in _RETRYABLE_CODES:
                        raise
                    last_exc = exc
                    if attempt == max_retries - 1:
                        logger.error(
                            "anthropic_retry: gave up after %d attempts (status=%s, func=%s)",
                            max_retries,
                            status,
                            func.__name__,
                        )
                        raise
                    delay = min(base_delay * (2**attempt) + random.uniform(0, jitter), max_delay)
                    logger.warning(
                        "anthropic_retry: status=%s attempt=%d/%d delay=%.1fs func=%s",
                        status,
                        attempt + 1,
                        max_retries,
                        delay,
                        func.__name__,
                    )
                    time.sleep(delay)
            raise last_exc  # unreachable but satisfies type checkers

        return wrapper  # type: ignore[return-value]

    return decorator


def _extract_status(exc: Exception) -> int | None:
    """HTTP status kodu çıkar — anthropic SDK ve requests/httpx hatalarını destekler."""
    # anthropic SDK: RateLimitError (429) ve APIStatusError (529 dahil)
    for attr in ("status_code", "http_status", "response"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
        if hasattr(val, "status_code"):
            return val.status_code
    # requests.HTTPError
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        return response.status_code
    return None
