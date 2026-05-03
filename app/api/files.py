"""File operations API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.file_manager import FileManager
from app.middleware.dependencies import rate_limit_read, rate_limit_write, require_auth, require_write
from app.models.schemas import (
    FileEditRequest,
    FileInfoResponse,
    FileReadResponse,
    FileWriteRequest,
)

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def get_file_manager(settings: Settings = Depends(get_settings)) -> FileManager:
    return FileManager(allowed_paths=settings.allowed_paths, max_file_size_mb=settings.max_file_size_mb)


@router.get(
    "/read",
    response_model=FileReadResponse,
    dependencies=[Depends(rate_limit_read), Depends(require_auth)],
)
async def read_file(path: str, offset: int = 0, limit: int = 1000, fm: FileManager = Depends(get_file_manager)):
    result = fm.read_file(path, offset=offset, limit=limit)
    return FileReadResponse(**result)


@router.put("/write", dependencies=[Depends(rate_limit_write), Depends(require_write)])
async def write_file(body: FileWriteRequest, fm: FileManager = Depends(get_file_manager)):
    return fm.write_file(body.path, body.content, body.mode)


@router.patch("/edit", dependencies=[Depends(rate_limit_write), Depends(require_write)])
async def edit_file(body: FileEditRequest, fm: FileManager = Depends(get_file_manager)):
    return fm.edit_file(body.path, body.old_string, body.new_string)


@router.delete("/delete", dependencies=[Depends(rate_limit_write), Depends(require_write)])
async def delete_file(path: str, fm: FileManager = Depends(get_file_manager)):
    fm.delete_file(path)
    return {"deleted": True}


@router.get("/info", response_model=FileInfoResponse, dependencies=[Depends(require_auth)])
async def file_info(path: str, fm: FileManager = Depends(get_file_manager)):
    return fm.get_file_info(path)
