"""Unit tests for the process-cohort continuous-agent leader lock."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import app.core.continuous_agent_leader as leader_module
from app.core.continuous_agent_leader import ContinuousAgentLeaderLock


class FakeFcntl:
    LOCK_EX = 0x02
    LOCK_NB = 0x04

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def flock(self, fd: int, operation: int) -> None:
        self.calls.append((fd, operation))
        if self.error is not None:
            raise self.error


def _mock_successful_os(monkeypatch: pytest.MonkeyPatch, *, fd: int = 41, pid: int = 1234) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "open": [],
        "set_inheritable": [],
        "fchmod": [],
        "ftruncate": [],
        "lseek": [],
        "write": [],
        "close": [],
    }

    def fake_open(path: str, flags: int, mode: int) -> int:
        calls["open"].append((path, flags, mode))
        return fd

    def fake_write(write_fd: int, data: bytes) -> int:
        calls["write"].append((write_fd, data))
        return len(data)

    monkeypatch.setattr(leader_module.os, "open", fake_open)
    monkeypatch.setattr(leader_module.os, "set_inheritable", lambda *args: calls["set_inheritable"].append(args))
    monkeypatch.setattr(leader_module.os, "fchmod", lambda *args: calls["fchmod"].append(args), raising=False)
    monkeypatch.setattr(leader_module.os, "ftruncate", lambda *args: calls["ftruncate"].append(args))
    monkeypatch.setattr(leader_module.os, "lseek", lambda *args: calls["lseek"].append(args))
    monkeypatch.setattr(leader_module.os, "write", fake_write)
    monkeypatch.setattr(leader_module.os, "close", lambda close_fd: calls["close"].append(close_fd))
    monkeypatch.setattr(leader_module.os, "getpid", lambda: pid)
    return calls


def test_successful_acquire_owns_secure_non_inheritable_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fcntl = FakeFcntl()
    monkeypatch.setattr(leader_module, "_fcntl", fake_fcntl)
    calls = _mock_successful_os(monkeypatch)

    lock = ContinuousAgentLeaderLock("cohort.lock")
    assert lock.try_acquire() == "leader"
    assert lock.role == "leader"
    assert lock.is_leader is True
    assert lock.fd == 41
    assert lock.error is None

    [(path, flags, mode)] = calls["open"]
    assert path == "cohort.lock"
    assert flags & os.O_CREAT
    assert flags & os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        assert flags & os.O_CLOEXEC
    assert mode == 0o600
    assert calls["set_inheritable"] == [(41, False)]
    assert calls["fchmod"] == [(41, 0o600)]
    assert fake_fcntl.calls == [(41, fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB)]
    assert calls["ftruncate"] == [(41, 0)]
    assert calls["lseek"] == [(41, 0, os.SEEK_SET)]
    assert calls["write"] == [(41, b"1234")]
    assert calls["close"] == []


def test_repeated_acquire_on_owned_instance_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fcntl = FakeFcntl()
    monkeypatch.setattr(leader_module, "_fcntl", fake_fcntl)
    calls = _mock_successful_os(monkeypatch)
    lock = ContinuousAgentLeaderLock("cohort.lock")

    assert lock.try_acquire() == "leader"
    assert lock.try_acquire() == "leader"
    assert len(calls["open"]) == 1
    assert len(fake_fcntl.calls) == 1


@pytest.mark.parametrize(
    "contention",
    [
        BlockingIOError(errno.EAGAIN, "already locked"),
        OSError(errno.EACCES, "already locked"),
    ],
)
def test_contention_becomes_standby_and_closes_candidate_fd(monkeypatch: pytest.MonkeyPatch, contention: OSError) -> None:
    fake_fcntl = FakeFcntl(contention)
    monkeypatch.setattr(leader_module, "_fcntl", fake_fcntl)
    calls = _mock_successful_os(monkeypatch)
    lock = ContinuousAgentLeaderLock("cohort.lock")

    assert lock.try_acquire() == "standby"
    assert lock.fd is None
    assert lock.is_leader is False
    assert lock.error is None
    assert calls["close"] == [41]
    assert calls["ftruncate"] == []
    assert calls["write"] == []


def test_missing_fcntl_is_fail_closed_without_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leader_module, "_fcntl", None)
    opened = False

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        nonlocal opened
        opened = True
        return 41

    monkeypatch.setattr(leader_module.os, "open", unexpected_open)
    lock = ContinuousAgentLeaderLock("cohort.lock")

    assert lock.try_acquire() == "lock_error"
    assert lock.fd is None
    assert lock.is_leader is False
    assert "fcntl is unavailable" in (lock.error or "")
    assert opened is False


def test_open_failure_is_lock_error_without_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leader_module, "_fcntl", FakeFcntl())
    closed: list[int] = []
    monkeypatch.setattr(leader_module.os, "open", lambda *_args: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    monkeypatch.setattr(leader_module.os, "close", closed.append)
    lock = ContinuousAgentLeaderLock("cohort.lock")

    assert lock.try_acquire() == "lock_error"
    assert lock.fd is None
    assert "PermissionError" in (lock.error or "")
    assert closed == []


@pytest.mark.parametrize("failure_point", ["set_inheritable", "fchmod", "ftruncate", "write"])
def test_failure_after_open_closes_fd_and_never_claims_leadership(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    fake_fcntl = FakeFcntl()
    monkeypatch.setattr(leader_module, "_fcntl", fake_fcntl)
    calls = _mock_successful_os(monkeypatch)

    if failure_point == "write":
        monkeypatch.setattr(leader_module.os, "write", lambda *_args: 0)
    else:
        monkeypatch.setattr(
            leader_module.os,
            failure_point,
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, f"{failure_point} failed")),
            raising=False,
        )

    lock = ContinuousAgentLeaderLock("cohort.lock")
    assert lock.try_acquire() == "lock_error"
    assert lock.fd is None
    assert lock.is_leader is False
    assert calls["close"] == [41]


def test_release_closes_fd_without_unlinking(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_fcntl = FakeFcntl()
    monkeypatch.setattr(leader_module, "_fcntl", fake_fcntl)
    calls = _mock_successful_os(monkeypatch)
    unlinked: list[str] = []
    monkeypatch.setattr(leader_module.os, "unlink", unlinked.append)
    lock = ContinuousAgentLeaderLock("cohort.lock")
    assert lock.try_acquire() == "leader"

    lock.release()
    lock.release()  # idempotent

    assert lock.role == "standby"
    assert lock.fd is None
    assert lock.is_leader is False
    assert calls["close"] == [41]
    assert unlinked == []


@pytest.mark.skipif(leader_module._fcntl is None, reason="requires production Linux fcntl semantics")
def test_real_linux_flock_mode_pid_takeover_and_file_persistence(tmp_path: Path) -> None:
    path = tmp_path / "cohort.lock"
    first = ContinuousAgentLeaderLock(path)
    second = ContinuousAgentLeaderLock(path)

    try:
        assert first.try_acquire() == "leader"
        assert first.fd is not None
        assert os.get_inheritable(first.fd) is False
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == str(os.getpid())

        assert second.try_acquire() == "standby"
        assert second.fd is None

        first.release()
        assert path.exists()
        assert second.try_acquire() == "leader"
    finally:
        first.release()
        second.release()

    assert path.exists()


@pytest.mark.skipif(leader_module._fcntl is None, reason="requires production Linux fcntl semantics")
def test_real_linux_process_exit_releases_leadership(tmp_path: Path) -> None:
    path = tmp_path / "process-cohort.lock"
    script = (
        "import sys\n"
        "from app.core.continuous_agent_leader import ContinuousAgentLeaderLock\n"
        "lock = ContinuousAgentLeaderLock(sys.argv[1])\n"
        "print(lock.try_acquire(), flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    contender = ContinuousAgentLeaderLock(path)
    try:
        assert child.stdout is not None
        child_role = child.stdout.readline().strip()
        if child_role != "leader":
            child.terminate()
            child.wait(timeout=5)
            assert child.stderr is not None
            pytest.fail(f"child failed to acquire leadership: role={child_role!r}, stderr={child.stderr.read()!r}")
        assert contender.try_acquire() == "standby"

        child.terminate()
        child.wait(timeout=5)
        assert contender.try_acquire() == "leader"
    finally:
        contender.release()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
