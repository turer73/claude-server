"""X-Memory-Key dependency."""
from __future__ import annotations

from fastapi import Header, HTTPException, status


def make_auth_dependency(api_key: str | None):
    """Build a FastAPI dependency that enforces X-Memory-Key.

    If `api_key` is falsy, auth is disabled entirely — useful for tests and
    purely local deployments behind another layer (mesh VPN, reverse proxy).
    """

    async def verify(x_memory_key: str | None = Header(default=None)) -> None:
        if not api_key:
            return
        if x_memory_key != api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing X-Memory-Key",
            )

    return verify
