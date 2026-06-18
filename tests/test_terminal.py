"""Tests for terminal manager and WebSocket terminal sessions."""

import asyncio

import pytest

from app.core.terminal_manager import _HAS_PTY, TerminalManager


@pytest.fixture
def tm():
    return TerminalManager(max_sessions=3)


def test_create_session(tm):
    sid = tm.create_session()
    assert sid is not None
    assert tm.count() == 1


def test_list_sessions_empty(tm):
    assert tm.list_sessions() == []


def test_list_sessions(tm):
    sid = tm.create_session()
    sessions = tm.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == sid
    assert "created_at" in sessions[0]


def test_destroy_session(tm):
    sid = tm.create_session()
    tm.destroy_session(sid)
    assert tm.count() == 0


def test_max_sessions(tm):
    for _ in range(3):
        tm.create_session()
    from app.exceptions import RateLimitError

    with pytest.raises(RateLimitError):
        tm.create_session()


def test_get_session(tm):
    sid = tm.create_session()
    session = tm.get_session(sid)
    assert session is not None
    assert session.session_id == sid


def test_get_session_not_found(tm):
    from app.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        tm.get_session("nonexistent")


@pytest.mark.anyio
async def test_execute_in_session(tm):
    sid = tm.create_session()
    session = tm.get_session(sid)
    result = await session.execute("echo hello-terminal")
    assert result["exit_code"] == 0
    assert "hello-terminal" in result["stdout"]


@pytest.mark.anyio
async def test_execute_with_error(tm):
    sid = tm.create_session()
    session = tm.get_session(sid)
    result = await session.execute('python3 -c "exit(42)"')
    assert result["exit_code"] == 42


@pytest.mark.anyio
async def test_execute_timeout(tm):
    sid = tm.create_session()
    session = tm.get_session(sid)
    result = await session.execute("sleep 5", timeout=1)
    assert result["exit_code"] == -1
    assert "Timeout" in result["stderr"]


def test_set_cwd_and_close_all(tm):
    sid1 = tm.create_session()
    tm.create_session()
    s = tm.get_session(sid1)
    s.set_cwd("/tmp")
    assert s._cwd == "/tmp"
    assert tm.count() == 2
    tm.close_all()
    assert tm.count() == 0


@pytest.mark.anyio
async def test_non_pty_session_io_is_noop(tm):
    """Without start_pty, the PTY I/O paths are inert (Windows-fallback branch)."""
    sid = tm.create_session()
    session = tm.get_session(sid)
    assert session.is_pty is False
    # read returns empty, write/resize are no-ops (no exception)
    assert await session.read_pty() == ""
    await session.write_pty("noop")
    session.resize(100, 40)


@pytest.mark.skipif(not _HAS_PTY, reason="PTY not supported on this platform")
@pytest.mark.anyio
async def test_real_pty_lifecycle(tm):
    """Exercise the real-PTY fork path: start, resize, write, read, close."""
    sid = tm.create_session()
    session = tm.get_session(sid)
    session.set_cwd("/tmp")
    await session.start_pty(cols=100, rows=40)
    assert session.is_pty is True
    assert session._pty_fd is not None
    assert session._pid is not None

    # resize the live PTY
    session.resize(120, 50)

    # write a command and read its echoed output back
    await session.write_pty("echo pty-marker-xyz\n")
    output = ""
    for _ in range(60):
        await asyncio.sleep(0.05)
        output += await session.read_pty()
        if "pty-marker-xyz" in output:
            break
    assert "pty-marker-xyz" in output

    # close tears down fd + child process
    session.close()
    assert session._pty_fd is None
    assert session._pid is None


@pytest.mark.skipif(not _HAS_PTY, reason="PTY not supported on this platform")
@pytest.mark.anyio
async def test_close_tolerates_already_closed_fd(tm):
    """close() swallows OSError if the fd was already closed out from under it."""
    import os

    sid = tm.create_session()
    session = tm.get_session(sid)
    await session.start_pty()
    os.close(session._pty_fd)  # close the master behind the manager's back
    session.close()  # must not raise despite the stale fd
    assert session._pty_fd is None
    assert session._pid is None


@pytest.mark.skipif(not _HAS_PTY, reason="PTY not supported on this platform")
@pytest.mark.anyio
async def test_destroy_session_closes_pty(tm):
    sid = tm.create_session()
    session = tm.get_session(sid)
    await session.start_pty()
    assert session.is_pty is True
    tm.destroy_session(sid)
    assert tm.count() == 0
    assert session._pty_fd is None
