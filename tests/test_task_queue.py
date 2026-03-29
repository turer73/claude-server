"""Tests for Task Queue API — enqueue, status, list tasks."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock

from tests.conftest import TEST_API_KEY


def _mock_queue(pending=None, recent=None, task=None):
    """Create a mock task queue."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value=1)
    queue.list_pending = AsyncMock(return_value=pending or [])
    queue.recent_tasks = recent or []
    queue.get_task = AsyncMock(return_value=task)
    queue.status = {"running": True, "pending": 0, "processing": None}
    return queue


@pytest.mark.anyio
async def test_enqueue_task(client, auth_headers, app):
    queue = _mock_queue()
    app.state.task_queue = queue
    resp = await client.post("/api/v1/tasks/enqueue", json={
        "type": "shell", "payload": {"command": "echo hello"},
    }, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == 1
    assert data["status"] == "pending"


@pytest.mark.anyio
async def test_enqueue_requires_admin(client, read_headers):
    resp = await client.post("/api/v1/tasks/enqueue", json={
        "type": "shell", "payload": {},
    }, headers=read_headers)
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_queue_status(client, auth_headers, app):
    queue = _mock_queue()
    app.state.task_queue = queue
    resp = await client.get("/api/v1/tasks/status", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is True


@pytest.mark.anyio
async def test_queue_status_not_initialized(client, auth_headers, app):
    app.state.task_queue = None
    resp = await client.get("/api/v1/tasks/status", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is False


@pytest.mark.anyio
async def test_pending_tasks(client, auth_headers, app):
    pending = [
        {"id": 1, "type": "shell", "status": "pending", "payload": {}},
        {"id": 2, "type": "backup", "status": "running", "payload": {}},
    ]
    queue = _mock_queue(pending=pending)
    app.state.task_queue = queue
    resp = await client.get("/api/v1/tasks/pending", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 2


@pytest.mark.anyio
async def test_recent_tasks(client, auth_headers, app):
    recent = [{"id": 1, "type": "shell", "status": "completed", "result": "ok"}]
    queue = _mock_queue(recent=recent)
    app.state.task_queue = queue
    resp = await client.get("/api/v1/tasks/recent", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 1


@pytest.mark.anyio
async def test_get_task_by_id(client, auth_headers, app):
    task = {"id": 5, "type": "deploy", "status": "completed", "result": "deployed"}
    queue = _mock_queue(task=task)
    app.state.task_queue = queue
    resp = await client.get("/api/v1/tasks/5", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == 5


@pytest.mark.anyio
async def test_get_task_not_found(client, auth_headers, app):
    queue = _mock_queue(task=None)
    app.state.task_queue = queue
    resp = await client.get("/api/v1/tasks/999", headers=auth_headers)
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.anyio
async def test_enqueue_no_queue(client, auth_headers, app):
    app.state.task_queue = None
    resp = await client.post("/api/v1/tasks/enqueue", json={
        "type": "shell", "payload": {},
    }, headers=auth_headers)
    assert resp.status_code == 200
    assert "error" in resp.json()
