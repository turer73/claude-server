"""Social Media Content API — manage Panola social content engine on VPS."""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings
from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/social", tags=["social"])

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_SSH = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {VPS_HOST}"
SOCIAL_DIR = "/opt/panola-social"
PYTHON = f"{SOCIAL_DIR}/venv/bin/python"
CLI = f"cd {SOCIAL_DIR} && {PYTHON} main.py"

_SAFE_ARG = re.compile(r"^[\w\sçşğüöıÇŞĞÜÖİ.,;:!?@#%&()\-+=/'\"]+$", re.UNICODE)


def _sanitize(value: str) -> str:
    """Sanitize user input for shell commands. Reject dangerous characters."""
    if not value or not _SAFE_ARG.match(value):
        raise ValueError(f"Geçersiz karakter içeriyor: {value[:50]}")
    # Escape single quotes for shell
    return value.replace("'", "'\\''")


async def _vps_run(command: str, timeout: int = 60) -> dict:
    """Run a command on VPS and return parsed output."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    ssh_cmd = f"{VPS_SSH} '{command}'"
    result = await executor.execute(ssh_cmd, timeout=timeout)

    # Try to parse JSON from output
    output = result.get("stdout", "")
    try:
        start = output.find("{")
        end = output.rfind("}") + 1
        if start >= 0 and end > start:
            result["data"] = json.loads(output[start:end])
        else:
            # Try array
            start = output.find("[")
            end = output.rfind("]") + 1
            if start >= 0 and end > start:
                result["data"] = json.loads(output[start:end])
    except json.JSONDecodeError:
        pass

    return result


# --- Content Generation ---

class GenerateRequest(BaseModel):
    product: str = "petvet"
    content_type: str = "single_image_tip"
    topic: Optional[str] = None
    pillar: Optional[str] = None
    context: Optional[str] = None


@router.post("/content/generate")
async def generate_content(req: GenerateRequest, _: None = Depends(require_admin)) -> dict:
    """Generate social media content using AI."""
    cmd = f'{CLI} generate --product {_sanitize(req.product)} --type {_sanitize(req.content_type)}'
    if req.topic:
        cmd += f" --topic '{_sanitize(req.topic)}'"
    if req.pillar:
        cmd += f' --pillar {_sanitize(req.pillar)}'
    if req.context:
        cmd += f" --context '{_sanitize(req.context)}'"
    return await _vps_run(cmd, timeout=90)


# --- Content Management ---

@router.get("/content/list")
async def list_contents(
    status: Optional[str] = Query(None),
    product: Optional[str] = Query(None),
    limit: int = Query(20),
    _: None = Depends(require_admin),
) -> dict:
    """List content items with optional filters."""
    cmd = f'{CLI} list'
    if status:
        cmd += f' --status {_sanitize(status)}'
    if product:
        cmd += f' --product {_sanitize(product)}'
    cmd += f' --limit {int(limit)}'
    return await _vps_run(cmd)


@router.get("/content/{content_id}")
async def get_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Get a specific content item."""
    cid = int(content_id)
    cmd = f"""{PYTHON} -c "
import json, sys; sys.path.insert(0, '{SOCIAL_DIR}')
from src.db import get_content
c = get_content({cid})
print(json.dumps(c, ensure_ascii=False, default=str))
" """
    return await _vps_run(f"cd {SOCIAL_DIR} && {cmd}")


class ApproveRequest(BaseModel):
    content_id: int


@router.put("/content/{content_id}/approve")
async def approve_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Approve a draft content item."""
    cmd = f'{CLI} approve --id {int(content_id)}'
    return await _vps_run(cmd)


class ScheduleRequest(BaseModel):
    scheduled_at: str  # ISO datetime


@router.put("/content/{content_id}/schedule")
async def schedule_content(
    content_id: int, req: ScheduleRequest, _: None = Depends(require_admin)
) -> dict:
    """Schedule content for future publishing."""
    cmd = f"{CLI} schedule --id {int(content_id)} --at '{_sanitize(req.scheduled_at)}'"
    return await _vps_run(cmd)


@router.post("/content/{content_id}/publish")
async def publish_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Publish a content item to Instagram."""
    cmd = f'{CLI} publish --id {int(content_id)}'
    return await _vps_run(cmd, timeout=120)


@router.post("/content/publish-scheduled")
async def publish_scheduled(_: None = Depends(require_admin)) -> dict:
    """Publish all content scheduled before now."""
    cmd = f'{CLI} publish-scheduled'
    return await _vps_run(cmd, timeout=120)


# --- Weekly Plans ---

class PlanRequest(BaseModel):
    product: str = "petvet"
    week_start: Optional[str] = None


@router.post("/plan/generate")
async def generate_plan(req: PlanRequest, _: None = Depends(require_admin)) -> dict:
    """Generate a weekly content plan."""
    cmd = f'{CLI} plan-week --product {_sanitize(req.product)}'
    if req.week_start:
        cmd += f' --week-start {_sanitize(req.week_start)}'
    return await _vps_run(cmd, timeout=90)


@router.get("/plan/calendar")
async def get_calendar(
    product: Optional[str] = Query(None),
    _: None = Depends(require_admin),
) -> dict:
    """Get content calendar view."""
    cmd = f'{CLI} calendar'
    if product:
        cmd += f' --product {_sanitize(product)}'
    return await _vps_run(cmd)


# --- Analytics ---

@router.get("/analytics/overview")
async def analytics_overview(_: None = Depends(require_admin)) -> dict:
    """Get analytics overview for last 7 days."""
    cmd = f'{CLI} report'
    return await _vps_run(cmd)


@router.get("/analytics/stats")
async def content_stats(_: None = Depends(require_admin)) -> dict:
    """Get content statistics."""
    cmd = f'{CLI} stats'
    return await _vps_run(cmd)


@router.post("/analytics/collect")
async def collect_metrics(_: None = Depends(require_admin)) -> dict:
    """Collect metrics from Instagram API."""
    cmd = f'{CLI} collect-metrics'
    return await _vps_run(cmd, timeout=60)


# --- Full Week Generation ---

class WeekGenerateRequest(BaseModel):
    product: str = "petvet"
    week_start: Optional[str] = None


@router.post("/content/generate-week")
async def generate_week(req: WeekGenerateRequest, _: None = Depends(require_admin)) -> dict:
    """Generate a full week of content (plan + all posts)."""
    cmd = f'{CLI} generate-week --product {_sanitize(req.product)}'
    if req.week_start:
        cmd += f' --week-start {_sanitize(req.week_start)}'
    return await _vps_run(cmd, timeout=300)


# --- Image Generation ---

@router.post("/content/{content_id}/generate-image")
async def generate_image(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Generate images for an existing content item."""
    cmd = f'{CLI} generate-image --id {int(content_id)}'
    return await _vps_run(cmd, timeout=60)


# --- Token Management ---

@router.get("/token/status")
async def token_status(_: None = Depends(require_admin)) -> dict:
    """Check Instagram token validity."""
    cmd = f'{CLI} token-check'
    return await _vps_run(cmd)


@router.post("/token/refresh")
async def token_refresh(_: None = Depends(require_admin)) -> dict:
    """Refresh Instagram token."""
    cmd = f'{CLI} token-auto'
    return await _vps_run(cmd, timeout=30)
