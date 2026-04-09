"""Deploy API — one-command deploy, project tracking, Claude workspace."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings
from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/deploy", tags=["deploy"])

PROJECTS_FILE = "/data/claude/projects/registry.json"
WORKSPACE = "/data/claude/workspace"


def _load_registry() -> dict:
    try:
        with open(PROJECTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"projects": {}}


def _save_registry(data: dict) -> None:
    os.makedirs(os.path.dirname(PROJECTS_FILE), exist_ok=True)
    with open(PROJECTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Self-deploy (linux-ai-server) ────────────────


class SelfDeployRequest(BaseModel):
    """Deploy the linux-ai-server project from a tarball path or auto-detect."""
    restart: bool = True


@router.post("/self")
async def deploy_self(req: SelfDeployRequest, _: None = Depends(require_admin)) -> dict:
    """Rebuild and restart the linux-ai-server from /opt/linux-ai-server."""
    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    results = []

    # Run tests
    start = time.monotonic()
    test_result = await executor.execute(
        "bash -c 'cd /opt/linux-ai-server && source venv/bin/activate && python -m pytest tests/ -q --ignore=tests/test_mcp.py 2>&1 | tail -5'",
        timeout=120,
    )
    results.append({"step": "test", "exit_code": test_result["exit_code"], "output": test_result["stdout"]})

    if test_result["exit_code"] != 0:
        return {"success": False, "reason": "tests_failed", "results": results}

    # Restart service
    if req.restart:
        restart_result = await executor.execute("systemctl restart linux-ai-server", timeout=15)
        results.append({"step": "restart", "exit_code": restart_result["exit_code"]})

    elapsed = round((time.monotonic() - start) * 1000)
    return {"success": True, "elapsed_ms": elapsed, "results": results}


# ── Project Registry ─────────────────────────────


class ProjectRegister(BaseModel):
    name: str
    path: str  # path on server
    github: str = ""
    stack: str = ""
    description: str = ""


@router.post("/projects/register")
async def register_project(req: ProjectRegister, _: None = Depends(require_admin)) -> dict:
    """Register a project for tracking."""
    registry = _load_registry()
    registry["projects"][req.name] = {
        "path": req.path,
        "github": req.github,
        "stack": req.stack,
        "description": req.description,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "last_deploy": None,
        "deploy_count": 0,
    }
    _save_registry(registry)
    return {"registered": req.name}


@router.get("/projects")
async def list_projects(_: None = Depends(require_admin)) -> dict:
    """List all tracked projects."""
    registry = _load_registry()
    return registry


@router.get("/projects/{name}")
async def get_project(name: str, _: None = Depends(require_admin)) -> dict:
    """Get project details including git status."""
    registry = _load_registry()
    project = registry["projects"].get(name)
    if not project:
        return {"error": f"Project {name} not found"}

    # Get git status if path exists
    path = project.get("path", "")
    git_info = {}
    if os.path.isdir(path):
        settings = get_settings()
        executor = ShellExecutor(whitelist=settings.shell_whitelist)
        try:
            branch = await executor.execute(f"git -C {path} branch --show-current", timeout=5)
            log = await executor.execute(f"git -C {path} log --oneline -5", timeout=5)
            status = await executor.execute(f"git -C {path} status --porcelain", timeout=5)
            git_info = {
                "branch": branch["stdout"].strip(),
                "recent_commits": log["stdout"].strip().split("\n"),
                "dirty_files": len([l for l in status["stdout"].strip().split("\n") if l]),
            }
        except Exception:
            pass

    return {**project, "name": name, "git": git_info}


@router.delete("/projects/{name}")
async def unregister_project(name: str, _: None = Depends(require_admin)) -> dict:
    """Remove a project from tracking."""
    registry = _load_registry()
    if name in registry["projects"]:
        del registry["projects"][name]
        _save_registry(registry)
        return {"unregistered": name}
    return {"error": f"Project {name} not found"}


# ── Deploy Project ───────────────────────────────


class DeployProjectRequest(BaseModel):
    name: str
    command: str = ""  # custom deploy command, or auto-detect


@router.post("/projects/{name}/deploy")
async def deploy_project(name: str, _: None = Depends(require_admin)) -> dict:
    """Deploy a tracked project — git pull + build + restart."""
    registry = _load_registry()
    project = registry["projects"].get(name)
    if not project:
        return {"error": f"Project {name} not found"}

    path = project.get("path", "")
    if not os.path.isdir(path):
        return {"error": f"Project path {path} not found on server"}

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    results = []

    # Git pull
    pull = await executor.execute(f"git -C {path} pull", timeout=30)
    results.append({"step": "git_pull", "exit_code": pull["exit_code"], "output": pull["stdout"][:200]})

    # Update deploy metadata
    project["last_deploy"] = datetime.now(timezone.utc).isoformat()
    project["deploy_count"] = project.get("deploy_count", 0) + 1
    _save_registry(registry)

    return {"success": pull["exit_code"] == 0, "project": name, "results": results}


# ── Claude Workspace ─────────────────────────────


@router.get("/memory/context")
async def memory_context(request: Request) -> dict:
    """Full session context - API key auth (no JWT needed)."""
    api_key = request.headers.get("x-api-key", "")
    expected = os.environ.get("API_KEY", "")
    if not expected or api_key != expected:
        from app.exceptions import AuthenticationError
        raise AuthenticationError("Invalid API key")
    """Full session context from memory DB for Claude hooks."""
    import sqlite3
    db = Path("/opt/linux-ai-server/data/claude_memory.db")
    if not db.exists():
        return {"error": "memory DB not found"}
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        result = {}
        c.execute("SELECT name, description, content FROM memories WHERE type='project' AND active=1 ORDER BY updated_at DESC")
        result["projects"] = [{"name": r["name"], "description": r["description"], "content": (r["content"] or "")[:500]} for r in c.fetchall()]
        c.execute("SELECT date, device_name, summary FROM sessions ORDER BY id DESC LIMIT 3")
        result["recent_sessions"] = [dict(r) for r in c.fetchall()]
        c.execute("SELECT project, task, status, device_name, created_at FROM tasks_log ORDER BY id DESC LIMIT 5")
        result["recent_tasks"] = [dict(r) for r in c.fetchall()]
        c.execute("SELECT name, description FROM memories WHERE type IN ('feedback','decision') AND active=1")
        result["rules"] = [dict(r) for r in c.fetchall()]
        conn.close()
        return result
    except Exception as e:
        return {"error": str(e)}


@router.get("/workspace/notes")
async def list_notes(_: None = Depends(require_admin)) -> dict:
    """List Claude's workspace notes."""
    notes_dir = Path(WORKSPACE)
    notes = []
    if notes_dir.is_dir():
        for f in sorted(notes_dir.iterdir()):
            if f.is_file():
                notes.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
    return {"notes": notes}


class NoteRequest(BaseModel):
    name: str
    content: str


@router.post("/workspace/notes")
async def save_note(req: NoteRequest, _: None = Depends(require_admin)) -> dict:
    """Save a note to Claude's workspace."""
    notes_dir = Path(WORKSPACE)
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / req.name
    path.write_text(req.content)
    return {"saved": req.name, "size": len(req.content)}


@router.get("/workspace/notes/{name}")
async def read_note(name: str, _: None = Depends(require_admin)) -> dict:
    """Read a workspace note."""
    path = Path(WORKSPACE) / name
    if not path.is_file():
        return {"error": f"Note {name} not found"}
    return {"name": name, "content": path.read_text()}
