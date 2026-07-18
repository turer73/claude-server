"""app/main.py — _acquire_remediation_leader_lock testleri (disc#1352 P0-fix, çok-worker
çift-remediation stopgap'i). Saf-ish fonksiyon: dosya-flock, DB/app bağımlılığı yok."""

from app.main import _acquire_remediation_leader_lock


def test_first_acquire_succeeds(tmp_path):
    lock_path = str(tmp_path / "leader.lock")
    fh = _acquire_remediation_leader_lock(lock_path)
    assert fh is not None
    fh.close()


def test_second_acquire_fails_while_first_held(tmp_path):
    # disc#1352: iki worker aynı-anda kalkarsa yalnız biri kilidi almalı
    lock_path = str(tmp_path / "leader.lock")
    fh1 = _acquire_remediation_leader_lock(lock_path)
    assert fh1 is not None
    fh2 = _acquire_remediation_leader_lock(lock_path)
    assert fh2 is None  # ikinci-worker non-leader — bloklamaz, anında döner
    fh1.close()


def test_acquire_succeeds_again_after_release(tmp_path):
    # lider-worker çökerse/yeniden-başlarsa kilit otomatik serbest kalmalı (OS-seviyesi)
    lock_path = str(tmp_path / "leader.lock")
    fh1 = _acquire_remediation_leader_lock(lock_path)
    fh1.close()  # process-exit simülasyonu → flock otomatik serbest
    fh2 = _acquire_remediation_leader_lock(lock_path)
    assert fh2 is not None
    fh2.close()


def test_creates_parent_directory(tmp_path):
    lock_path = str(tmp_path / "nested" / "dir" / "leader.lock")
    fh = _acquire_remediation_leader_lock(lock_path)
    assert fh is not None
    fh.close()
