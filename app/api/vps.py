"""VPS Bridge API — control production VPS from dev server."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings
from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/vps", tags=["vps"])

def _vps_ssh():
    s = get_settings()
    return f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {s.vps_host}"


class VPSCommandRequest(BaseModel):
    command: str
    timeout: int = 30


@router.post("/exec")
async def vps_exec(req: VPSCommandRequest, _: None = Depends(require_admin)) -> dict:
    """Execute a command on the production VPS via SSH bridge."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    # Wrap command in SSH
    ssh_cmd = f"{_vps_ssh()} '{req.command}'"
    result = await executor.execute(ssh_cmd, timeout=req.timeout)
    return result


@router.get("/status")
async def vps_status(_: None = Depends(require_admin)) -> dict:
    """Get VPS system status — hostname, uptime, resources, containers."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)

    cmd = f"""{_vps_ssh()} 'echo HOSTNAME=$(hostname) && echo UPTIME=$(uptime -p) && echo CPU=$(nproc) && free -h | awk "/Mem/{{print \\"RAM_USED=\\"\\$3\\"/\\"\\$2}}" && df -h / | awk "NR==2{{print \\"DISK=\\"\\$3\\"/\\"\\$2\\" (\\"\\$5\\")\\"}}" && docker ps --format "CONTAINER={{{{.Names}}}}:{{{{.Status}}}}" 2>/dev/null | head -20'"""

    result = await executor.execute(cmd, timeout=15)
    if result["exit_code"] != 0:
        return {"online": False, "error": result["stderr"]}

    # Parse output
    lines = result["stdout"].strip().split("\n")
    info = {}
    containers = []
    for line in lines:
        if line.startswith("CONTAINER="):
            parts = line[10:].split(":", 1)
            containers.append({"name": parts[0], "status": parts[1] if len(parts) > 1 else "?"})
        elif "=" in line:
            k, v = line.split("=", 1)
            info[k.lower()] = v

    return {"online": True, **info, "containers": containers}


@router.get("/services")
async def vps_services(_: None = Depends(require_admin)) -> dict:
    """Check VPS web services health."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)

    services = {
        "coolify": "https://coolify.panola.app",
        "uptime": "https://uptime.panola.app",
        "n8n": "https://n8n.panola.app",
        "analytics": "https://analytics.panola.app",
    }

    cmd = f"""{_vps_ssh()} '{" && ".join([f'echo "{name}=$(curl -s -o /dev/null -w %{{http_code}} {url})"' for name, url in services.items()])}'"""

    result = await executor.execute(cmd, timeout=20)
    parsed = {}
    for line in result["stdout"].strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k] = {"url": services.get(k, ""), "http_code": int(v) if v.isdigit() else 0}

    return {"services": parsed}


@router.post("/deploy/{project}")
async def vps_deploy(project: str, _: None = Depends(require_admin)) -> dict:
    """Trigger a deploy on VPS via Coolify webhook or git pull."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)

    # For now: git pull on VPS if project dir exists
    project_paths = {
        "panola": "/opt/panola-baas",
    }

    path = project_paths.get(project)
    if path:
        cmd = f"{_vps_ssh()} 'cd {path} && git pull && docker-compose restart 2>&1 | tail -5'"
        result = await executor.execute(cmd, timeout=60)
        return {"project": project, "deployed": result["exit_code"] == 0, "output": result["stdout"]}

    return {"error": f"Unknown project: {project}. Known: {list(project_paths.keys())}"}
