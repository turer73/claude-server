"""Social Media Content API — manage Panola social content engine on VPS."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings
from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/social", tags=["social"])

VPS_HOST = "root@REDACTED_VPS_IP"
VPS_SSH = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {VPS_HOST}"
SOCIAL_DIR = "/opt/panola-social"
PYTHON = f"{SOCIAL_DIR}/venv/bin/python"
CLI = f"cd {SOCIAL_DIR} && {PYTHON} main.py"


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
    cmd = f'{CLI} generate --product {req.product} --type {req.content_type}'
    if req.topic:
        cmd += f' --topic "{req.topic}"'
    if req.pillar:
        cmd += f' --pillar {req.pillar}'
    if req.context:
        cmd += f' --context "{req.context}"'
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
        cmd += f' --status {status}'
    if product:
        cmd += f' --product {product}'
    cmd += f' --limit {limit}'
    return await _vps_run(cmd)


@router.get("/content/{content_id}")
async def get_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Get a specific content item."""
    cmd = f"""{PYTHON} -c "
import json, sys; sys.path.insert(0, '{SOCIAL_DIR}')
from src.db import get_content
c = get_content({content_id})
print(json.dumps(c, ensure_ascii=False, default=str))
" """
    return await _vps_run(f"cd {SOCIAL_DIR} && {cmd}")


class ApproveRequest(BaseModel):
    content_id: int


@router.put("/content/{content_id}/approve")
async def approve_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Approve a draft content item."""
    cmd = f'{CLI} approve --id {content_id}'
    return await _vps_run(cmd)


class ScheduleRequest(BaseModel):
    scheduled_at: str  # ISO datetime


@router.put("/content/{content_id}/schedule")
async def schedule_content(
    content_id: int, req: ScheduleRequest, _: None = Depends(require_admin)
) -> dict:
    """Schedule content for future publishing."""
    cmd = f'{CLI} schedule --id {content_id} --at "{req.scheduled_at}"'
    return await _vps_run(cmd)


@router.post("/content/{content_id}/publish")
async def publish_content(content_id: int, _: None = Depends(require_admin)) -> dict:
    """Publish a content item to Instagram."""
    cmd = f'{CLI} publish --id {content_id}'
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
    cmd = f'{CLI} plan-week --product {req.product}'
    if req.week_start:
        cmd += f' --week-start {req.week_start}'
    return await _vps_run(cmd, timeout=90)


@router.get("/plan/calendar")
async def get_calendar(
    product: Optional[str] = Query(None),
    _: None = Depends(require_admin),
) -> dict:
    """Get content calendar view."""
    cmd = f'{CLI} calendar'
    if product:
        cmd += f' --product {product}'
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
    cmd = f'{CLI} generate-week --product {req.product}'
    if req.week_start:
        cmd += f' --week-start {req.week_start}'
    return await _vps_run(cmd, timeout=300)
