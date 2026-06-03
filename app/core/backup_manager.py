"""Backup manager — create, list, restore, delete backups."""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime

from app.exceptions import NotFoundError


def _validate_tar_members(tar: tarfile.TarFile, dest: str) -> None:
    """tar üyelerini path-traversal + unsafe-link'e karşı doğrula (saf; yazma yok).
    Py<3.11.4 fallback'i için (filter param yok). Kötü üye -> TarError. Codex #28."""
    dest_real = os.path.realpath(dest)
    for m in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, m.name))
        if target != dest_real and not target.startswith(dest_real + os.sep):
            raise tarfile.TarError(f"unsafe path in tar (traversal): {m.name}")
        if m.issym() or m.islnk():
            raise tarfile.TarError(f"unsafe link in tar: {m.name}")


def _safe_extractall(tar: tarfile.TarFile, dest: str) -> None:
    """tar path-traversal'a karşı güvenli extract. Py3.11.4+/3.12+ -> filter="data"
    (PEP 706). Daha eski 3.11.x'te filter param YOK -> manuel member-validation."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(path=dest, filter="data")
        return
    _validate_tar_members(tar, dest)
    tar.extractall(path=dest)  # pragma: no cover (yalnız Py<3.11.4; modern'de erişilmez)


def _is_sqlite_file(path: str) -> bool:
    """Return True if file is a SQLite database (magic header check)."""
    if not path.endswith(".db") or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _snapshot_sqlite(src: str, dst: str) -> None:
    """Consistent online backup via SQLite backup API — no lock race."""
    with sqlite3.connect(src) as src_conn, sqlite3.connect(dst) as dst_conn:
        src_conn.backup(dst_conn)


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
        """Tarball backup. SQLite .db files use online backup API to avoid
        mid-transaction snapshots (fixes concurrent 'database is locked' race
        with live writers). -wal/-shm sidecars are excluded — they're only
        consistent when paired with the main DB they belong to.
        """
        os.makedirs(self._backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{label}_{ts}.tar.gz" if label else f"backup_{ts}.tar.gz"
        path = os.path.join(self._backup_dir, name)

        with tempfile.TemporaryDirectory(prefix="bkp-snap-") as snap_dir:
            # Map source path -> arcname for items added to tar.
            # SQLite files get a consistent snapshot into snap_dir first.
            with tarfile.open(path, "w:gz") as tar:
                for source in self._sources:
                    if not os.path.exists(source):
                        continue
                    src_base = os.path.basename(source.rstrip(os.sep))
                    if os.path.isdir(source):
                        for entry in os.listdir(source):
                            full = os.path.join(source, entry)
                            arcname = os.path.join(src_base, entry)
                            # Skip WAL/SHM sidecars — captured by online backup
                            if entry.endswith((".db-wal", ".db-shm")):
                                continue
                            if _is_sqlite_file(full):
                                snap_path = os.path.join(snap_dir, entry)
                                try:
                                    _snapshot_sqlite(full, snap_path)
                                    tar.add(snap_path, arcname=arcname)
                                except sqlite3.Error:
                                    # Snapshot failed (corrupt?) — fall back to raw add
                                    tar.add(full, arcname=arcname)
                            else:
                                tar.add(full, arcname=arcname)
                    else:
                        # Single file source
                        if _is_sqlite_file(source):
                            snap_path = os.path.join(snap_dir, src_base)
                            try:
                                _snapshot_sqlite(source, snap_path)
                                tar.add(snap_path, arcname=src_base)
                            except sqlite3.Error:
                                tar.add(source, arcname=src_base)
                        else:
                            tar.add(source, arcname=src_base)

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
                backups.append(
                    {
                        "filename": f,
                        "path": full,
                        "size": st.st_size,
                        "created": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    }
                )
        return backups

    def restore_backup(self, backup_path: str, target_dir: str) -> bool:
        if not os.path.isfile(backup_path):
            raise NotFoundError(f"Backup not found: {backup_path}")
        os.makedirs(target_dir, exist_ok=True)
        with tarfile.open(backup_path, "r:gz") as tar:
            _safe_extractall(tar, target_dir)
        return True

    def delete_backup(self, backup_path: str) -> bool:
        if not os.path.isfile(backup_path):
            raise NotFoundError(f"Backup not found: {backup_path}")
        os.remove(backup_path)
        return True
