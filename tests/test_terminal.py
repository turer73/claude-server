"""Tests for terminal manager and WebSocket terminal sessions."""

import pytest
from app.core.terminal_manager import TerminalManager, TerminalSession


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
    result = await session.execute("python3 -c \"exit(42)\"")
    assert result["exit_code"] == 42
