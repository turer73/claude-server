"""Shell execution API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.shell_executor import ShellExecutor
from app.models.schemas import ShellExecRequest, ShellExecResponse

router = APIRouter(prefix="/api/v1/shell", tags=["shell"])


def get_shell_executor(settings: Settings = Depends(get_settings)) -> ShellExecutor:
    return ShellExecutor(whitelist=settings.shell_whitelist)


@router.post("/exec", response_model=ShellExecResponse)
async def exec_command(body: ShellExecRequest, executor: ShellExecutor = Depends(get_shell_executor)):
    result = await executor.execute(body.command, timeout=body.timeout, cwd=body.cwd)
    return ShellExecResponse(**result)
