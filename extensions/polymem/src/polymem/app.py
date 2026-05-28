"""FastAPI app + router factory."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI

from polymem.auth import make_auth_dependency
from polymem.db import bootstrap_schema
from polymem.routes.devices import build_router as build_devices_router
from polymem.routes.memories import build_router as build_memories_router
from polymem.routes.sessions import build_router as build_sessions_router


def create_router(
    *,
    db_path: str | Path,
    api_key: str | None,
    bootstrap: bool = True,
) -> APIRouter:
    """Return a router mountable into an existing FastAPI app.

    Args:
        db_path:   SQLite file path. Created if missing.
        api_key:   X-Memory-Key value clients must send. Pass falsy to disable
                   auth entirely (only safe behind another perimeter).
        bootstrap: If True (default), runs the schema bootstrap before mounting.
    """
    if bootstrap:
        bootstrap_schema(db_path)

    auth_dep = make_auth_dependency(api_key)

    parent = APIRouter()
    parent.include_router(build_memories_router(db_path, auth_dep))
    parent.include_router(build_devices_router(db_path, auth_dep))
    parent.include_router(build_sessions_router(db_path, auth_dep))
    # Slice 3: search, alembic-driven migrations
    return parent


def create_app(*, db_path: str | Path, api_key: str | None, bootstrap: bool = True) -> FastAPI:
    """Build a minimal standalone FastAPI app — useful for `uvicorn polymem.app:app` style runs."""
    app = FastAPI(title="polymem", version="0.1.0")
    app.include_router(create_router(db_path=db_path, api_key=api_key, bootstrap=bootstrap))
    return app
