"""Backup/Restore API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.backup_manager import BackupManager
from app.middleware.dependencies import require_admin

router = APIRouter(prefix="/api/v1/backup", tags=["backup"])

_manager = BackupManager(
    source_dirs=["/var/AI-stump/", "/etc/linux-ai-server/"],
    backup_dir="/var/lib/linux-ai-server/backups",
)


@router.post("/create", dependencies=[Depends(require_admin)])
async def create_backup(label: str = ""):
    return _manager.create_backup(label=label)


@router.get("/list", dependencies=[Depends(require_admin)])
async def list_backups():
    return {"backups": _manager.list_backups()}


@router.post("/restore", dependencies=[Depends(require_admin)])
async def restore_backup(backup_path: str, target_dir: str = "/tmp/restore"):
    _manager.restore_backup(backup_path, target_dir)
    return {"restored": True, "target": target_dir}


@router.delete("/delete", dependencies=[Depends(require_admin)])
async def delete_backup(backup_path: str):
    _manager.delete_backup(backup_path)
    return {"deleted": True}
