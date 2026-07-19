"""Process-cohort leadership for continuous background agents.

The lock is intentionally small and policy-free: it only elects one process by
holding a non-blocking Linux ``flock``.  The caller owns agent lifecycle and
must keep this object alive for as long as leadership is required.

The fixed default filename is shared with the legacy consciousness lock.  That
preserves the consciousness singleton during a mixed-version transition, but
legacy releases did not put the other four agents behind this lock.  Deploying
the cohort therefore requires an all-worker service restart, not a rolling
mixed-version replacement.
"""

from __future__ import annotations

import errno
import os
import tempfile
from typing import Literal, Protocol, cast


class _FcntlApi(Protocol):
    LOCK_EX: int
    LOCK_NB: int

    def flock(self, fd: int, operation: int) -> object: ...


try:
    import fcntl as _fcntl_module
except ImportError:  # pragma: no cover - exercised through the portable mock test
    _fcntl_module = None  # type: ignore[assignment]

_fcntl = cast("_FcntlApi | None", _fcntl_module)

ContinuousAgentRole = Literal["leader", "standby", "lock_error"]

DEFAULT_CONTINUOUS_AGENT_LOCK_PATH = os.path.join(tempfile.gettempdir(), "consciousness-worker.lock")


def _set_fd_mode(fd: int, mode: int) -> None:
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        raise OSError(errno.ENOSYS, "fchmod is unavailable")
    fchmod(fd, mode)


class ContinuousAgentLeaderLock:
    """Own a process-scoped, non-blocking cohort leader lock.

    ``try_acquire`` is fail-closed: contention becomes ``standby`` while an
    unsupported platform or any filesystem/lock metadata failure becomes
    ``lock_error``.  A successful instance owns its file descriptor until
    ``release`` or process exit.  Releasing closes the descriptor but never
    removes the lock file; unlinking a flock file can create split lock domains.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = os.fspath(path) if path is not None else DEFAULT_CONTINUOUS_AGENT_LOCK_PATH
        self._fd: int | None = None
        self._role: ContinuousAgentRole = "standby"
        self._error: str | None = None

    @property
    def fd(self) -> int | None:
        """The owned descriptor, exposed read-only for diagnostics."""
        return self._fd

    @property
    def role(self) -> ContinuousAgentRole:
        return self._role

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def is_leader(self) -> bool:
        return self._role == "leader" and self._fd is not None

    def try_acquire(self) -> ContinuousAgentRole:
        """Try once without blocking and return leader/standby/lock_error."""
        if self._fd is not None:
            self._role = "leader"
            self._error = None
            return self._role

        self._error = None
        if _fcntl is None:
            self._role = "lock_error"
            self._error = "fcntl is unavailable; continuous-agent leadership requires Linux flock"
            return self._role

        fd: int | None = None
        try:
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            # O_CLOEXEC is requested above where supported; this explicit call
            # also covers platforms/builds where the flag is absent or ignored.
            os.set_inheritable(fd, False)
            # os.open's mode does not change an already-existing file.
            _set_fd_mode(fd, 0o600)
        except OSError as exc:
            self._close_unowned_fd(fd)
            return self._set_lock_error(exc)

        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            self._close_unowned_fd(fd)
            if isinstance(exc, BlockingIOError) or exc.errno in {errno.EACCES, errno.EAGAIN}:
                self._role = "standby"
                self._error = None
                return self._role
            return self._set_lock_error(exc)

        try:
            pid = str(os.getpid()).encode("ascii")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            written = os.write(fd, pid)
            if written != len(pid):
                raise OSError(errno.EIO, f"partial PID write: {written}/{len(pid)} bytes")
        except OSError as exc:
            # Closing releases the flock acquired above; no failed attempt may
            # retain an untracked descriptor or leadership.
            self._close_unowned_fd(fd)
            return self._set_lock_error(exc)

        self._fd = fd
        self._role = "leader"
        self._error = None
        return self._role

    def release(self) -> None:
        """Release owned leadership by closing the fd; keep the file in place."""
        if self._fd is None:
            return

        fd = self._fd
        self._fd = None
        try:
            os.close(fd)
        except OSError as exc:
            self._set_lock_error(exc)
            return
        self._role = "standby"
        self._error = None

    def _set_lock_error(self, exc: OSError) -> ContinuousAgentRole:
        self._role = "lock_error"
        self._error = f"{type(exc).__name__}: {exc}"
        return self._role

    @staticmethod
    def _close_unowned_fd(fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass


__all__ = [
    "DEFAULT_CONTINUOUS_AGENT_LOCK_PATH",
    "ContinuousAgentLeaderLock",
    "ContinuousAgentRole",
]
