"""Project health tracking API — tüm projelerin durumunu tek yerden izle."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends

from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

PROJECTS = [
    {"name": "linux-ai-server", "path": "/opt/linux-ai-server", "type": "python"},
    {"name": "panola", "path": "/data/projects/panola", "type": "node"},
    {"name": "bilge-arena", "path": "/data/projects/bilge-arena", "type": "node"},
    {"name": "renderhane", "path": "/data/projects/renderhane", "type": "node"},
    {"name": "koken-akademi", "path": "/data/projects/koken-akademi", "type": "node"},
    {"name": "kuafor", "path": "/data/projects/kuafor", "type": "node"},
    {"name": "petvet", "path": "/data/projects/petvet", "type": "node"},
    {"name": "demo-saas", "path": "/data/projects/demo-saas", "type": "node"},
]


def _git_info(path: str) -> dict:
    """Son commit bilgisi."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%s|%ai|%an"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|", 3)
            return {
                "sha": parts[0][:8],
                "message": parts[1] if len(parts) > 1 else "",
                "date": parts[2] if len(parts) > 2 else "",
                "author": parts[3] if len(parts) > 3 else "",
            }
    except Exception:
        pass
    return {}


def _git_status(path: str) -> dict:
    """Uncommitted changes count."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        return {
            "branch": branch.stdout.strip(),
            "dirty_files": len(lines),
        }
    except Exception:
        return {}


def _dep_audit(path: str, project_type: str) -> dict:
    """Dependency security audit."""
    try:
        if project_type == "python":
            # pip-audit or safety check
            return {"status": "skip", "note": "use pip-audit manually"}

        # Node projects — npm audit
        pkg = Path(path) / "package-lock.json"
        if not pkg.exists():
            # Check subdirectories for monorepo
            for sub in ["worker", "web", "apps/api"]:
                pkg = Path(path) / sub / "package-lock.json"
                if pkg.exists():
                    path = str(pkg.parent)
                    break

        if not pkg.exists():
            return {"status": "skip", "note": "no lockfile"}

        result = subprocess.run(
            ["npm", "audit", "--json", "--omit=dev"],
            cwd=path, capture_output=True, text=True, timeout=30,
        )
        try:
            data = json.loads(result.stdout)
            vulns = data.get("metadata", {}).get("vulnerabilities", {})
            total = sum(vulns.get(k, 0) for k in ["high", "critical"])
            return {
                "status": "ok" if total == 0 else "warning",
                "high": vulns.get("high", 0),
                "critical": vulns.get("critical", 0),
            }
        except (json.JSONDecodeError, KeyError):
            return {"status": "unknown"}
    except Exception:
        return {"status": "error"}


def _last_test_result() -> Optional[dict]:
    """En son test runner sonucu."""
    import glob
    files = sorted(glob.glob("/tmp/test-results-*.json"), reverse=True)
    if files:
        try:
            with open(files[0]) as f:
                return json.load(f)
        except Exception:
            pass
    return None


@router.get("/health")
async def project_health(_=Depends(require_auth)):
    """Tüm projelerin sağlık durumu."""
    projects = []
    for proj in PROJECTS:
        path = proj["path"]
        exists = os.path.isdir(path)
        info = {
            "name": proj["name"],
            "path": path,
            "exists": exists,
        }
        if exists:
            info["git"] = _git_info(path)
            info["git_status"] = _git_status(path)
        projects.append(info)

    test_result = _last_test_result()

    return {
        "timestamp": datetime.now().isoformat(),
        "projects": projects,
        "last_test_run": test_result,
    }


@router.get("/audit")
async def dependency_audit(_=Depends(require_auth)):
    """Tüm projelerin dependency güvenlik taraması."""
    results = {}
    for proj in PROJECTS:
        if os.path.isdir(proj["path"]):
            results[proj["name"]] = _dep_audit(proj["path"], proj["type"])
    return {"timestamp": datetime.now().isoformat(), "audits": results}


@router.post("/sync")
async def sync_repos(_=Depends(require_auth)):
    """Git pull tüm projeler."""
    results = {}
    for proj in PROJECTS:
        path = proj["path"]
        if not os.path.isdir(path):
            results[proj["name"]] = {"status": "skip", "reason": "not found"}
            continue
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=path, capture_output=True, text=True, timeout=30,
            )
            results[proj["name"]] = {
                "status": "ok" if result.returncode == 0 else "error",
                "output": result.stdout.strip()[:200],
            }
        except Exception as e:
            results[proj["name"]] = {"status": "error", "reason": str(e)}
    return {"timestamp": datetime.now().isoformat(), "results": results}
