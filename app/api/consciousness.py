"""Consciousness API — stream/self/status endpoints.

Provides access to the consciousness stream's thoughts and self-model.
Follows same auth pattern as devops.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/consciousness", tags=["consciousness"])


def _get_stream(request: Request) -> Any:
    return getattr(request.app.state, "consciousness_stream", None)


@router.get("/status")
async def stream_status(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    stream = _get_stream(request)
    if not stream:
        return {"running": False, "thought_count": 0, "error": "not_initialized"}
    return stream.status


@router.get("/stream")
async def recent_thoughts(
    request: Request,
    limit: int = 30,
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    stream = _get_stream(request)
    if not stream:
        return {"thoughts": [], "error": "not_initialized"}
    thoughts = stream.get_recent_thoughts(limit=limit)
    return {"thoughts": thoughts, "count": len(thoughts)}


@router.get("/self")
async def self_model(request: Request, _: None = Depends(require_auth)) -> dict[str, Any]:
    """Current self-model: aggregated state + emotional trend + focus."""
    stream = _get_stream(request)
    if not stream:
        return {"error": "not_initialized"}
    return stream.get_self_model()
