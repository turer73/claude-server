"""Güvenlik batch-2 regresyon guard'ları (Codex audit, ORTA bulgular).

Her test bir bulgunun KAPALI olduğunu kilitler: file_manager sibling-prefix,
backup tar-traversal, projects/sync write-perm, deploy note path-traversal.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from app.core.backup_manager import BackupManager
from app.core.file_manager import FileManager
from app.exceptions import AuthorizationError


def test_file_manager_sibling_prefix_blocked(tmp_path):
    """/tmp/foo izinliyse /tmp/foobar/secret GEÇMEMELI (sibling-prefix bug)."""
    allowed = tmp_path / "foo"
    allowed.mkdir()
    sibling = tmp_path / "foobar"
    sibling.mkdir()
    (sibling / "secret").write_text("x")

    fm = FileManager(allowed_paths=[str(allowed)])
    with pytest.raises(AuthorizationError):
        fm.validate_path(str(sibling / "secret"))
    # gerçek alt-yol HÂLÂ izinli (regresyon: fix fazla-kısıtlamasın)
    (allowed / "ok").write_text("y")
    assert fm.validate_path(str(allowed / "ok"))


def test_backup_restore_blocks_tar_traversal(tmp_path):
    """../ içeren kötü-niyetli tar target_dir DIŞINA yazamamalı (filter=data)."""
    malicious = tmp_path / "evil.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    bm = BackupManager(source_dirs=[str(tmp_path)], backup_dir=str(tmp_path), retention_days=7)
    target = tmp_path / "restore-target"
    # filter='data' traversal'i reddeder -> tarfile.TarError (OutsideDestinationError vb).
    with pytest.raises(tarfile.TarError):
        bm.restore_backup(str(malicious), str(target))
    # target-DIŞINA kaçış dosyası YAZILMAMIŞ olmalı
    assert not (tmp_path / "escape.txt").exists()


async def test_projects_sync_requires_write(client, read_headers):
    """/projects/sync (git pull = mutasyon) read-perm ile YAPILAMAMALI -> 403."""
    resp = await client.post("/api/v1/projects/sync", headers=read_headers)
    assert resp.status_code == 403  # require_write; read-JWT yetmez (git-pull tetiklenmez)


async def test_deploy_note_rejects_path_traversal(client, auth_headers, monkeypatch, tmp_path):
    """workspace note `name` ../ ile WORKSPACE dışına YAZAMAMALI -> 400 (admin-only + sanitize)."""
    monkeypatch.setattr("app.api.deploy.WORKSPACE", str(tmp_path / "ws"))
    resp = await client.post(
        "/api/v1/deploy/workspace/notes",
        json={"name": "../../etc/evil", "content": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    # geçerli isim HÂLÂ çalışır
    ok = await client.post(
        "/api/v1/deploy/workspace/notes",
        json={"name": "ok.txt", "content": "y"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
