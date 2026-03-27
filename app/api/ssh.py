"""SSH API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.ssh_client import SSHClient, SSHSessionManager
from app.middleware.dependencies import require_admin
from app.models.schemas import SshConnectRequest, SshExecRequest, SshExecResponse

router = APIRouter(prefix="/api/v1/ssh", tags=["ssh"])

# Singleton session manager
_session_mgr = SSHSessionManager(max_sessions=5)
_ssh_client = SSHClient()


@router.post("/connect", dependencies=[Depends(require_admin)])
async def ssh_connect(body: SshConnectRequest):
    client = _ssh_client.connect(
        host=body.host,
        username=body.username,
        password=body.password,
        port=body.port,
        key_path=body.key_path,
    )
    sid = _session_mgr.add(body.host, body.username, client)
    return {"session_id": sid, "host": body.host, "status": "connected"}


@router.post("/exec", response_model=SshExecResponse, dependencies=[Depends(require_admin)])
async def ssh_exec(body: SshExecRequest):
    client = _session_mgr.get(body.session_id)
    result = _ssh_client.exec_command(client, body.command, timeout=body.timeout)
    return SshExecResponse(**result)


@router.get("/sessions", dependencies=[Depends(require_admin)])
async def ssh_sessions():
    return {"sessions": _session_mgr.list_sessions()}


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def ssh_disconnect(session_id: str):
    _session_mgr.remove(session_id)
    return {"disconnected": True}
