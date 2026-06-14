import os

import pytest

from app.core.backup_manager import BackupManager


@pytest.fixture
def bm(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yml").write_text("key: value")
    (source / "data.json").write_text('{"x": 1}')
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return BackupManager(
        source_dirs=[str(source)],
        backup_dir=str(backup_dir),
        retention_days=7,
    )


def test_create_backup(bm):
    result = bm.create_backup()
    assert result["success"] is True
    assert "path" in result
    assert os.path.isfile(result["path"])
    assert result["path"].endswith(".tar.gz")


def test_create_backup_atomic_no_tmp(bm):
    # Başarılı backup sonrası hiçbir .tmp artığı kalmamalı (restore-test *.tar.gz
    # glob'una partial arşiv sızmasın — atomik yayın).
    result = bm.create_backup()
    assert result["success"] is True
    files = os.listdir(bm._backup_dir)
    assert all(not f.endswith(".tmp") for f in files), files
    assert any(f.endswith(".tar.gz") for f in files)


def test_create_backup_error_cleans_tmp(bm, monkeypatch):
    # Yazım yarıda patlarsa .tmp temizlenmeli + final arşiv oluşmamalı.
    import app.core.backup_manager as bmgr

    def boom(path, *a, **k):
        open(path, "wb").close()  # yarım .tmp arşivi simüle et
        raise RuntimeError("disk full")

    monkeypatch.setattr(bmgr.tarfile, "open", boom)
    with pytest.raises(RuntimeError):
        bm.create_backup()
    leftovers = os.listdir(bm._backup_dir)
    assert leftovers == [], f"temizlenmeyen artık: {leftovers}"


def test_list_backups_empty(tmp_path):
    bm = BackupManager(source_dirs=[], backup_dir=str(tmp_path / "empty"))
    backups = bm.list_backups()
    assert backups == []


def test_list_backups(bm):
    bm.create_backup()
    backups = bm.list_backups()
    assert len(backups) == 1
    assert "filename" in backups[0]
    assert "size" in backups[0]
    assert "created" in backups[0]


def test_multiple_backups(bm):
    bm.create_backup()
    bm.create_backup()
    backups = bm.list_backups()
    assert len(backups) == 2


def test_restore_backup(bm, tmp_path):
    result = bm.create_backup()
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    bm.restore_backup(result["path"], str(restore_dir))
    # Should have extracted files
    assert any(restore_dir.iterdir())


def test_delete_backup(bm):
    result = bm.create_backup()
    bm.delete_backup(result["path"])
    assert not os.path.exists(result["path"])


def test_backup_size(bm):
    result = bm.create_backup()
    assert result["size_bytes"] > 0
