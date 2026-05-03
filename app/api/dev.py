"""Development tools API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.dev_manager import DevManager
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import GitCommitRequest, GitStatusResponse

router = APIRouter(prefix="/api/v1/dev", tags=["dev"])


def get_dev_manager() -> DevManager:
    return DevManager()


@router.get("/git/status", response_model=GitStatusResponse, dependencies=[Depends(require_auth)])
async def git_status(cwd: str = Query(...), dm: DevManager = Depends(get_dev_manager)):
    return dm.git_status(cwd)


@router.get("/git/log", dependencies=[Depends(require_auth)])
async def git_log(cwd: str = Query(...), limit: int = 10, dm: DevManager = Depends(get_dev_manager)):
    return {"entries": dm.git_log(cwd, limit=limit)}


@router.get("/git/diff", dependencies=[Depends(require_auth)])
async def git_diff(cwd: str = Query(...), dm: DevManager = Depends(get_dev_manager)):
    return {"diff": dm.git_diff(cwd)}


@router.post("/git/commit", dependencies=[Depends(require_write)])
async def git_commit(body: GitCommitRequest, cwd: str = Query(...), dm: DevManager = Depends(get_dev_manager)):
    if body.files:
        dm.git_add(cwd, body.files)
    dm.git_commit(cwd, body.message)
    return {"committed": True}
