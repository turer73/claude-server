"""Unit tests for TaskQueue class — direct testing with real temp DB."""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.task_queue import TaskQueue, TaskResult
from app.db.database import Database


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    db = Database(str(tmp_path / "queue.db"))
    await db.initialize()
    yield db
    await db.close()
    get_settings.cache_clear()


@pytest.fixture
def queue(db):
    return TaskQueue(db=db)


@pytest.mark.anyio
async def test_enqueue_returns_id(queue):
    task_id = await queue.enqueue("shell", {"command": "echo hello"})
    assert task_id > 0


@pytest.mark.anyio
async def test_enqueue_no_db():
    q = TaskQueue(db=None)
    result = await q.enqueue("shell", {})
    assert result == -1


@pytest.mark.anyio
async def test_get_task(queue):
    task_id = await queue.enqueue("shell", {"command": "ls"})
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["type"] == "shell"
    assert task["status"] == "pending"


@pytest.mark.anyio
async def test_get_task_not_found(queue):
    task = await queue.get_task(999)
    assert task is None


@pytest.mark.anyio
async def test_get_task_no_db():
    q = TaskQueue(db=None)
    result = await q.get_task(1)
    assert result is None


@pytest.mark.anyio
async def test_list_pending(queue):
    await queue.enqueue("shell", {"command": "echo 1"})
    await queue.enqueue("backup", {})
    pending = await queue.list_pending()
    assert len(pending) == 2
    assert pending[0]["type"] == "shell"


@pytest.mark.anyio
async def test_list_pending_no_db():
    q = TaskQueue(db=None)
    result = await q.list_pending()
    assert result == []


def test_status_property(queue):
    assert queue.status["running"] is False
    assert queue.status["processed"] == 0


def test_recent_tasks_empty(queue):
    assert queue.recent_tasks == []


def test_recent_tasks_populated(queue):
    queue._recent.append(TaskResult(task_id=1, type="shell", status="completed", result="ok", elapsed_ms=50.0))
    queue._recent.append(TaskResult(task_id=2, type="backup", status="failed", result="err", elapsed_ms=100.0))
    recent = queue.recent_tasks
    assert len(recent) == 2
    assert recent[0]["task_id"] == 2  # reversed order


@pytest.mark.anyio
async def test_process_next_shell(queue, db):
    mock_result = {"stdout": "hello\n", "stderr": "", "exit_code": 0, "elapsed_ms": 10}
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, return_value=mock_result):
        await queue.enqueue("shell", {"command": "echo hello"})
        await queue._process_next()

    assert queue._processed == 1
    assert len(queue._recent) == 1
    assert queue._recent[0].status == "completed"


@pytest.mark.anyio
async def test_process_next_shell_failure(queue, db):
    mock_result = {"stdout": "", "stderr": "error", "exit_code": 1, "elapsed_ms": 10}
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, return_value=mock_result):
        await queue.enqueue("shell", {"command": "false"})
        await queue._process_next()

    assert queue._recent[0].status == "failed"


@pytest.mark.anyio
async def test_process_next_unknown_type(queue, db):
    await queue.enqueue("unknown_type", {})
    await queue._process_next()
    assert queue._recent[0].status == "failed"
    assert "Unknown job type" in queue._recent[0].result


@pytest.mark.anyio
async def test_process_next_no_pending(queue, db):
    await queue._process_next()
    assert queue._processed == 0


@pytest.mark.anyio
async def test_process_next_no_db():
    q = TaskQueue(db=None)
    await q._process_next()
    assert q._processed == 0


@pytest.mark.anyio
async def test_process_next_exception(queue, db):
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        await queue.enqueue("shell", {"command": "echo x"})
        await queue._process_next()

    assert queue._recent[0].status == "failed"


@pytest.mark.anyio
async def test_start_stop(queue):
    queue.start()
    assert queue._running is True
    # Start again should be no-op
    queue.start()
    await queue.stop()
    assert queue._running is False


@pytest.mark.anyio
async def test_process_vps_exec(queue, db):
    mock_result = {"stdout": "vps output\n", "stderr": "", "exit_code": 0, "elapsed_ms": 200}
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, return_value=mock_result):
        await queue.enqueue("vps_exec", {"command": "hostname"})
        await queue._process_next()

    assert queue._recent[0].status == "completed"


@pytest.mark.anyio
async def test_process_backup(queue, db):
    mock_result = {"stdout": "backup done\n", "stderr": "", "exit_code": 0, "elapsed_ms": 500}
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, return_value=mock_result):
        await queue.enqueue("backup", {})
        await queue._process_next()

    assert queue._recent[0].status == "completed"


@pytest.mark.anyio
async def test_result_truncation(queue, db):
    long_output = "x" * 5000
    mock_result = {"stdout": long_output, "stderr": "", "exit_code": 0, "elapsed_ms": 10}
    with patch.object(queue._executor, "execute", new_callable=AsyncMock, return_value=mock_result):
        await queue.enqueue("shell", {"command": "echo x"})
        await queue._process_next()

    # recent_tasks truncates to 200 chars
    assert len(queue.recent_tasks[0]["result"]) == 200
