"""X-Memory-Key dependency."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status


def make_auth_dependency(api_key: str | None):
    """Build a FastAPI dependency that enforces X-Memory-Key.

    If `api_key` is falsy, auth is disabled entirely — useful for tests and
    purely local deployments behind another layer (mesh VPN, reverse proxy).
    """

    async def verify(x_memory_key: str | None = Header(default=None)) -> None:
        if not api_key:
            return
        # Constant-time karşılaştırma — erken-çıkış timing-attack ile key brute-force'u önler.
        # Bytes'a encode et: hmac.compare_digest str'lerde non-ASCII'de TypeError raise eder
        # (örn. X-Memory-Key='é') → 500 yerine temiz 401 dönsün (Codex P2).
        if x_memory_key is None or not hmac.compare_digest(x_memory_key.encode("utf-8"), api_key.encode("utf-8")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing X-Memory-Key",
            )

    return verify
