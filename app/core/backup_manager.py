"""Backup manager — create, list, restore, delete backups."""

from __future__ import annotations

import os
import tarfile
from datetime import datetime
from pathlib import Path

from app.exceptions import NotFoundError


class BackupManager:
    def __init__(
        self,
        source_dirs: list[str],
        backup_dir: str = "/var/lib/linux-ai-server/backups",
        retention_days: int = 7,
    ) -> None:
        self._sources = source_dirs
        self._backup_dir = backup_dir
        self._retention = retention_days

    def create_backup(self, label: str = "") -> dict:
        os.makedirs(self._backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{label}_{ts}.tar.gz" if label else f"backup_{ts}.tar.gz"
        path = os.path.join(self._backup_dir, name)

        with tarfile.open(path, "w:gz") as tar:
            for source in self._sources:
                if os.path.exists(source):
                    arcname = os.path.basename(source)
                    tar.add(source, arcname=arcname)

        size = os.path.getsize(path)
        return {
            "success": True,
            "path": path,
            "filename": name,
            "size_bytes": size,
            "created": datetime.now().isoformat(),
        }

    def list_backups(self) -> list[dict]:
        if not os.path.isdir(self._backup_dir):
            return []
        backups = []
        for f in sorted(os.listdir(self._backup_dir), reverse=True):
            if f.endswith(".tar.gz"):
                full = os.path.join(self._backup_dir, f)
                st = os.stat(full)
                backups.append({
                    "filename": f,
                    "path": full,
                    "size": st.st_size,
                    "created": datetime.fromtimestamp(st.st_mtime).isoformat(),
                })
        return backups

    def restore_backup(self, backup_path: str, target_dir: str) -> bool:
        if not os.path.isfile(backup_path):
            raise NotFoundError(f"Backup not found: {backup_path}")
        os.makedirs(target_dir, exist_ok=True)
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(path=target_dir)
        return True

    def delete_backup(self, backup_path: str) -> bool:
        if not os.path.isfile(backup_path):
            raise NotFoundError(f"Backup not found: {backup_path}")
        os.remove(backup_path)
        return True
