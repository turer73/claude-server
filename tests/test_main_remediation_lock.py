"""app/main.py — _acquire_remediation_leader_lock testleri (disc#1352 P0-fix, çok-worker
çift-remediation stopgap'i). Saf-ish fonksiyon: dosya-flock (raw fd), DB/app bağımlılığı yok."""

import os

from app.main import _acquire_remediation_leader_lock


def test_first_acquire_succeeds(tmp_path):
    lock_path = str(tmp_path / "leader.lock")
    fd = _acquire_remediation_leader_lock(lock_path)
    assert fd is not None
    os.close(fd)


def test_second_acquire_fails_while_first_held(tmp_path):
    # disc#1352: iki worker aynı-anda kalkarsa yalnız biri kilidi almalı
    lock_path = str(tmp_path / "leader.lock")
    fd1 = _acquire_remediation_leader_lock(lock_path)
    assert fd1 is not None
    fd2 = _acquire_remediation_leader_lock(lock_path)
    assert fd2 is None  # ikinci-worker non-leader — bloklamaz, anında döner
    os.close(fd1)


def test_acquire_succeeds_again_after_release(tmp_path):
    # lider-worker çökerse/yeniden-başlarsa kilit otomatik serbest kalmalı (OS-seviyesi)
    lock_path = str(tmp_path / "leader.lock")
    fd1 = _acquire_remediation_leader_lock(lock_path)
    os.close(fd1)  # process-exit simülasyonu → flock otomatik serbest
    fd2 = _acquire_remediation_leader_lock(lock_path)
    assert fd2 is not None
    os.close(fd2)


def test_default_path_uses_tempdir_not_opt(monkeypatch):
    # Codex-P1 (PR#334): default artık tempfile.gettempdir() altında (consciousness.py'deki
    # _try_worker_lock deseniyle birebir) — ProtectSystem=strict+ReadWritePaths sertleştirmesinde
    # /opt/... yazılamaz, /tmp servise-özel (PrivateTmp) her iki dağıtımda da yazılabilir.
    fd = _acquire_remediation_leader_lock()
    try:
        assert fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def test_missing_parent_dir_returns_none_not_raises(tmp_path):
    # os.open(O_CREAT) os.makedirs GİBİ ara-dizin OLUŞTURMAZ — yeni implementasyon bunu
    # kasıtlı yapmıyor (tempfile.gettempdir() zaten düz-var). Eksik-üst-dizin de crash
    # DEĞİL, gracefully None dönmeli (FileNotFoundError bir OSError alt-sınıfı).
    lock_path = str(tmp_path / "hic-olmayan" / "dizin" / "leader.lock")
    fd = _acquire_remediation_leader_lock(lock_path)  # ASLA fırlamamalı
    assert fd is None


def test_unwritable_path_returns_none_not_raises(tmp_path):
    # Codex-P1 (PR#334): kilit-yolu yazılamıyorsa (ör. ProtectSystem=strict altında /opt
    # ReadWritePaths dışında) fonksiyon PermissionError FIRLATMAMALI — lifespan bunu
    # unconditional çağırıyor, fırlarsa TÜM app boot'u çöker. os.makedirs/open TEK
    # try/except(BlockingIOError, OSError) içinde olmalı, yalnız flock değil.
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o444)  # yazma-izni yok
    lock_path = str(ro_dir / "leader.lock")
    try:
        fd = _acquire_remediation_leader_lock(lock_path)  # ASLA fırlamamalı
        assert fd is None  # yazılamadı → gracefully None (crash değil)
    finally:
        ro_dir.chmod(0o755)  # tmp_path temizliği için geri-yaz-izni ver
