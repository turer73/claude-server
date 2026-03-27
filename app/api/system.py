"""System management API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.system_manager import SystemManager
from app.models.schemas import SystemInfoResponse, ProcessInfo, ProcessListResponse

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def get_system_manager() -> SystemManager:
    return SystemManager()


@router.get("/info", response_model=SystemInfoResponse)
async def system_info(mgr: SystemManager = Depends(get_system_manager)):
    info = mgr.get_system_info()
    return SystemInfoResponse(**info)


@router.get("/processes", response_model=ProcessListResponse)
async def process_list(
    limit: int = Query(default=20, ge=1, le=100),
    sort_by: str = Query(default="cpu", pattern="^(cpu|memory)$"),
    mgr: SystemManager = Depends(get_system_manager),
):
    procs = mgr.get_processes(limit=limit, sort_by=sort_by)
    return ProcessListResponse(
        processes=[ProcessInfo(**p) for p in procs],
        total=len(procs),
    )
