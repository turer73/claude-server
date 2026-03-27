"""WebOps API proxy endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings, get_settings
from app.core.webops_proxy import WebOpsProxy

router = APIRouter(prefix="/api/v1/webops", tags=["webops"])


def get_webops(settings: Settings = Depends(get_settings)) -> WebOpsProxy:
    return WebOpsProxy(
        vercel_token=settings.vercel_token,
        cloudflare_token=settings.cloudflare_token,
        github_token=settings.github_token,
        supabase_url=settings.supabase_url,
        supabase_token=settings.supabase_token,
        coolify_url=settings.coolify_url,
        coolify_token=settings.coolify_token,
    )


@router.get("/services")
async def list_services(proxy: WebOpsProxy = Depends(get_webops)):
    return {"services": proxy.available_services()}


@router.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_request(
    service: str,
    path: str,
    request: Request,
    proxy: WebOpsProxy = Depends(get_webops),
):
    body = await request.body()
    result = await proxy.request(
        service=service,
        method=request.method,
        path=f"/{path}",
        body=body.decode() if body else None,
    )
    return result
