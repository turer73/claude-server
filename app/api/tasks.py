"""Task Queue API — enqueue background work, check status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _get_queue(request: Request):
    return getattr(request.app.state, "task_queue", None)


class EnqueueRequest(BaseModel):
    type: str  # shell, vps_exec, deploy, backup
    payload: dict = {}


@router.post("/enqueue")
async def enqueue_task(req: EnqueueRequest, request: Request, _: None = Depends(require_admin)) -> dict:
    """Add a task to the background queue."""
    queue = _get_queue(request)
    if not queue:
        return {"error": "Task queue not initialized"}
    task_id = await queue.enqueue(req.type, req.payload)
    return {"task_id": task_id, "type": req.type, "status": "pending"}


@router.get("/status")
async def queue_status(request: Request, _: None = Depends(require_admin)) -> dict:
    """Get task queue status."""
    queue = _get_queue(request)
    if not queue:
        return {"running": False}
    return queue.status


@router.get("/pending")
async def pending_tasks(request: Request, _: None = Depends(require_admin)) -> dict:
    """List pending/running tasks."""
    queue = _get_queue(request)
    if not queue:
        return {"tasks": []}
    tasks = await queue.list_pending()
    return {"tasks": tasks}


@router.get("/recent")
async def recent_tasks(request: Request, _: None = Depends(require_admin)) -> dict:
    """Get recently completed tasks."""
    queue = _get_queue(request)
    if not queue:
        return {"tasks": []}
    return {"tasks": queue.recent_tasks}


@router.get("/{task_id}")
async def get_task(task_id: int, request: Request, _: None = Depends(require_admin)) -> dict:
    """Get a specific task by ID."""
    queue = _get_queue(request)
    if not queue:
        return {"error": "Queue not available"}
    task = await queue.get_task(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}
    return task
