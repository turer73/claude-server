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


# --- Yedekleme yolu canli DB'ye YAZAMAZ (2026-08-31 server.db bozulmasi) ---
#
# Bozulma penceresinde (03:00:39 backup 200 OK -> 03:00:47 "disk I/O error" ->
# 03:37 "malformed") donanim/disk-dolu/fd/RAM olcumle elendi. Mekanizma
# kanitlanamadi; ama _snapshot_sqlite CANLI DB'yi okuma-yazma aciyordu ve
# baglantiyi kapatmiyordu. Bu testler o yolu kalici olarak kapatir.


@pytest.fixture
def db_bm(tmp_path):
    """Icinde gercek bir SQLite DB olan kaynak dizinli BackupManager."""
    import sqlite3

    source = tmp_path / "source"
    source.mkdir()
    db_path = source / "live.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(50)])
    con.commit()
    con.close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bm = BackupManager(source_dirs=[str(source)], backup_dir=str(backup_dir))
    return bm, str(db_path)


def _spy_connect(monkeypatch):
    """sqlite3.connect cagrilarini yakala; gercek baglantiyi dondur."""
    import sqlite3

    import app.core.backup_manager as bmod

    calls: list[tuple[str, dict]] = []
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        calls.append((str(target), kwargs))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(bmod.sqlite3, "connect", spy)
    return calls


def test_snapshot_opens_source_readonly(db_bm, monkeypatch):
    """Kaynak DB mode=ro ile acilmali — yedek yolu canli DB'ye yazamamali."""
    bm, db_path = db_bm
    calls = _spy_connect(monkeypatch)

    assert bm.create_backup()["success"] is True

    # Yalniz KAYNAK acilislari; snapshot HEDEFI de "live.db" adini tasir ve
    # yazilabilir acilmalidir (yazilacak dosya odur) — onu disarida birak.
    source_opens = [(t, kw) for t, kw in calls if db_path in t]
    assert source_opens, f"kaynak DB hic acilmadi: {calls}"
    for target, kwargs in source_opens:
        assert target.startswith("file:"), f"URI degil, yazilabilir acilis: {target}"
        assert "mode=ro" in target, f"read-only DEGIL: {target}"
        assert kwargs.get("uri") is True, f"uri=True eksik: {target} {kwargs}"


def test_snapshot_closes_connections(db_bm, monkeypatch):
    """`with sqlite3.connect(...)` CLOSE ETMEZ — kapanis acik olmali."""
    import sqlite3

    bm, _ = db_bm
    opened: list[sqlite3.Connection] = []

    import app.core.backup_manager as bmod

    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        con = real(target, *args, **kwargs)
        opened.append(con)
        return con

    monkeypatch.setattr(bmod.sqlite3, "connect", spy)
    assert bm.create_backup()["success"] is True
    assert opened, "hic baglanti acilmadi"

    for con in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


def test_snapshot_tempdir_on_disk_not_tmpfs(db_bm, monkeypatch):
    """Snapshot /tmp'ye (tmpfs=RAM) degil, backup_dir'e (disk) yazilmali."""
    import tempfile

    import app.core.backup_manager as bmod

    seen: list[dict] = []
    real_td = tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_td(*args, **kwargs)

    monkeypatch.setattr(bmod.tempfile, "TemporaryDirectory", spy)
    bm, _ = db_bm
    assert bm.create_backup()["success"] is True

    assert seen, "TemporaryDirectory hic cagrilmadi"
    assert seen[0].get("dir") == bm._backup_dir, seen


def test_snapshot_content_is_valid_and_complete(db_bm, tmp_path):
    """Read-only snapshot gercek, butun ve okunabilir bir DB uretmeli."""
    import sqlite3
    import tarfile

    bm, _ = db_bm
    result = bm.create_backup()
    out = tmp_path / "extracted"
    out.mkdir()
    with tarfile.open(result["path"]) as tar:
        tar.extractall(out)  # noqa: S202 - test fixture, kendi urettigimiz arsiv

    restored = next(out.rglob("live.db"))
    con = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    finally:
        con.close()


# --- Olu kopyalar arsive girmemeli (2026-09-02 olcumu) ---
#
# data/ 13 GB'in 9,8 GB'i eski DB kopyasiydi (*.memsyn-bak.* 7,4 GB, *.corrupt-* 2,4 GB)
# ve gecelik yedek hepsini HER GECE yeniden tar'liyordu: tarball 755 MB -> 1,05 GB.
# Bunlar SILINMEZ (adli kanit), yalnizca arsive alinmaz.


def _dead_copy_bm(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "server.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    (source / "canli.txt").write_text("canli veri")
    for dead in (
        "claude_memory.db.memsyn-bak.1788158700",
        "server.db.corrupt-20260809-134445",
        "server.db.lostfound-20260815",
        "server.db.precorrupt-20260828",
        "server.db.preswap-20260614",
        "server.yml.bak-jwtrotate-20260603",
        "server.db.backup.1787842507",
    ):
        (source / dead).write_text("olu kopya " * 100)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return BackupManager(source_dirs=[str(source)], backup_dir=str(backup_dir))


def _members(path: str) -> list[str]:
    import tarfile

    with tarfile.open(path) as tar:
        return tar.getnames()


def test_dead_copies_excluded_from_archive(tmp_path):
    """Olu-kopya desenleri arsive GIRMEZ, canli dosyalar girer."""
    bm = _dead_copy_bm(tmp_path)
    result = bm.create_backup()
    names = " ".join(_members(result["path"]))

    for dead in ("memsyn-bak", "corrupt-", "lostfound-", "precorrupt", "preswap-", "bak-", "backup."):
        assert dead not in names, f"olu kopya arsive girdi ({dead}): {names}"
    assert "source/server.db" in names, names
    assert "source/canli.txt" in names, names


def test_dead_copy_skip_is_counted_not_silent(tmp_path):
    """Sessiz eleme YOK — atlanan sayisi ve boyutu raporlanmali."""
    bm = _dead_copy_bm(tmp_path)
    result = bm.create_backup()

    assert result["skipped_dead_copies"] == 7, result
    assert result["skipped_bytes"] > 0, result


def test_live_db_names_never_match_dead_patterns():
    """Canli DB adlari hicbir olu-kopya desenine UYMAMALI (yanlis-dislama korumasi)."""
    from app.core.backup_manager import _is_dead_copy

    for live in ("server.db", "claude_memory.db", "coverage.db", "rag_metrics.db", "server.yml"):
        assert not _is_dead_copy(live), f"canli dosya olu-kopya sanildi: {live}"


def test_exclusion_can_be_disabled(tmp_path, monkeypatch):
    """BACKUP_NO_EXCLUDE=1 kacis kapisi — her sey arsive girer."""
    monkeypatch.setenv("BACKUP_NO_EXCLUDE", "1")
    bm = _dead_copy_bm(tmp_path)
    result = bm.create_backup()

    assert result["skipped_dead_copies"] == 0, result
    assert "memsyn-bak" in " ".join(_members(result["path"]))
