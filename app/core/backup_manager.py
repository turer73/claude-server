"""Backup manager — create, list, restore, delete backups."""

from __future__ import annotations

import logging
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from typing import Any

from app.exceptions import NotFoundError

logger = logging.getLogger(__name__)


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
    """Consistent online backup via SQLite backup API — no lock race.

    Kaynak READ-ONLY (`mode=ro`) acilir: yedekleme yolu canli DB'ye yazma yetkili
    bir baglanti ACMAZ. Onceki hali `sqlite3.connect(src)` ile okuma-yazma
    aciyordu, yani gecelik yedek canli server.db'ye yazabilen bir yoldu
    (checkpoint/WAL-reset dahil). 2026-08-31 bozulmasinda mekanizma
    KANITLANAMADI, ama bu yolu tamamen elemek maliyetsiz.

    Baglantilar try/finally ile ACIKCA kapatilir: Python'da
    `with sqlite3.connect(...)` islemi commit/rollback eder, CLOSE ETMEZ —
    eski hali bu yuzden kapanisi GC'ye birakiyordu.

    Geri dusus: yazarsiz bir WAL DB'de `mode=ro` acilamayabilir (shm yok).
    O durumda eski okuma-yazma davranisina donulur — yazar yoksa risk de yoktur —
    ve cagiran taraf ham `tar.add` fallback'ine dusmez (tutarsiz kopya olurdu).
    """
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    except sqlite3.Error:
        logger.warning("snapshot: %s read-only acilamadi, rw'ye dusuluyor", src)
        src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


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

    def create_backup(self, label: str = "") -> dict[str, Any]:
        """Tarball backup. SQLite .db files use online backup API to avoid
        mid-transaction snapshots (fixes concurrent 'database is locked' race
        with live writers). -wal/-shm sidecars are excluded — they're only
        consistent when paired with the main DB they belong to.
        """
        os.makedirs(self._backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{label}_{ts}.tar.gz" if label else f"backup_{ts}.tar.gz"
        path = os.path.join(self._backup_dir, name)
        # Atomik yayın (Codex): arşivi önce .tmp'ye yaz, tamamlanınca os.replace ile
        # yerine koy. Aksi halde yarıda-kesilen yazım, restore-test'in `*.tar.gz`
        # glob'una (ve cleanup `ls -t`'ye) PARTIAL arşiv olarak sızıp false-FAIL üretir.
        # `.tar.gz.tmp` glob'a takılmaz → in-progress arşiv görünmez.
        tmp_path = path + ".tmp"

        # gzip level: default 1 (fast). 03:00 cron backup'ta gzip-9 ~11x daha
        # fazla CPU yakıyordu (3.3s vs 0.3s) -> 85% eşiğini aşıp CRITICAL alert
        # üretiyordu. Boyut farkı yedek başına ~2MB (lokal + VPS'e çekiliyor).
        # BACKUP_GZIP_LEVEL ile override edilebilir (0-9).
        try:
            gzip_level = int(os.environ.get("BACKUP_GZIP_LEVEL", "1"))
        except ValueError:
            gzip_level = 1
        gzip_level = min(9, max(1, gzip_level))

        # Snapshot dizini DISKTE olmali, /tmp'de DEGIL: /tmp burada tmpfs (RAM).
        # claude_memory.db tek basina 1.8 GB — varsayilan /tmp kullanildiginda her
        # gece bu kadar veri RAM'e yaziliyordu. backup_dir zaten yukarida
        # makedirs edildi ve ciktinin kendisiyle ayni dosya sisteminde.
        with tempfile.TemporaryDirectory(prefix="bkp-snap-", dir=self._backup_dir) as snap_dir:
            # Map source path -> arcname for items added to tar.
            # SQLite files get a consistent snapshot into snap_dir first.
            with tarfile.open(tmp_path, "w:gz", compresslevel=gzip_level) as tar:
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
        # Atomik yayın (Codex): tar tamamlanınca .tmp'yi os.replace ile yerine koy →
        # restore-test'in *.tar.gz glob'u yarıda-kalan arşivi GÖRMEZ (.tar.gz.tmp eşleşmez).
        # Hata'da .tmp kalabilir ama glob'a takılmaz (zararsız); daily-backup zaten fail-alert atar.
        os.replace(tmp_path, path)

        size = os.path.getsize(path)
        return {
            "success": True,
            "path": path,
            "filename": name,
            "size_bytes": size,
            "created": datetime.now().isoformat(),
        }

    def list_backups(self) -> list[dict[str, Any]]:
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
            # GÜVENLIK: filter="data" (PEP 706) tar path-traversal'i engeller (mutlak/
            # ../-kaçış engellenir, link-target'ları doğrulanır). requires-python>=3.11.4
            # garanti eder (Codex #28). Eski plain extractall target_dir DIŞINA yazabilirdi.
            tar.extractall(path=target_dir, filter="data")
        return True

    def delete_backup(self, backup_path: str) -> bool:
        if not os.path.isfile(backup_path):
            raise NotFoundError(f"Backup not found: {backup_path}")
        os.remove(backup_path)
        return True
