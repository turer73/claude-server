"""Network API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.network_proxy import NetworkProxy
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import HttpProxyRequest, HttpProxyResponse

router = APIRouter(prefix="/api/v1/network", tags=["network"])


def get_network_proxy() -> NetworkProxy:
    return NetworkProxy()


@router.post(
    "/request",
    response_model=HttpProxyResponse,
    dependencies=[Depends(require_write)],
)
async def proxy_request(body: HttpProxyRequest, proxy: NetworkProxy = Depends(get_network_proxy)):
    result = await proxy.http_request(
        method=body.method, url=body.url, headers=body.headers, body=body.body, timeout=body.timeout,
    )
    return HttpProxyResponse(**result)


@router.get("/interfaces", dependencies=[Depends(require_auth)])
async def get_interfaces(proxy: NetworkProxy = Depends(get_network_proxy)):
    return {"interfaces": proxy.get_interfaces()}


@router.get("/connections", dependencies=[Depends(require_auth)])
async def get_connections(proxy: NetworkProxy = Depends(get_network_proxy)):
    return {"connections": proxy.get_connections()}


@router.post("/dns", dependencies=[Depends(require_auth)])
async def dns_lookup(hostname: str, proxy: NetworkProxy = Depends(get_network_proxy)):
    return {"hostname": hostname, "records": await proxy.dns_lookup(hostname)}
