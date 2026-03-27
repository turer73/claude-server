"""Kernel control API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.kernel_bridge import KernelBridge
from app.middleware.dependencies import rate_limit_read, rate_limit_write, require_auth, require_write
from app.models.schemas import GovernorRequest, GovernorResponse, KernelStatusResponse

router = APIRouter(prefix="/api/v1/kernel", tags=["kernel"])


def get_kernel_bridge() -> KernelBridge:
    return KernelBridge()


@router.get(
    "/status",
    response_model=KernelStatusResponse,
    dependencies=[Depends(rate_limit_read), Depends(require_auth)],
)
async def kernel_status(bridge: KernelBridge = Depends(get_kernel_bridge)):
    status = bridge.get_status()
    return KernelStatusResponse(
        state=status["state"],
        governor=status["governor"],
        cpu_count=status["cpu_count"],
        services=status["services"],
        version=status.get("version"),
    )


@router.get(
    "/governor",
    response_model=GovernorResponse,
    dependencies=[Depends(rate_limit_read), Depends(require_auth)],
)
async def get_governor(bridge: KernelBridge = Depends(get_kernel_bridge)):
    status = bridge.get_status()
    return GovernorResponse(governor=status["governor"])


@router.put(
    "/governor",
    response_model=GovernorResponse,
    dependencies=[Depends(rate_limit_write), Depends(require_write)],
)
async def set_governor(body: GovernorRequest, bridge: KernelBridge = Depends(get_kernel_bridge)):
    bridge.set_governor(body.mode)
    return GovernorResponse(governor=body.mode, cpu_mask=body.cpu_mask)
