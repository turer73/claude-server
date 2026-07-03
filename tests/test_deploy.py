"""Tests for Deploy API — self-deploy, project registry, workspace notes."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_deploy_self_tests_pass(client, auth_headers):
    mock_test = {"stdout": "10 passed", "stderr": "", "exit_code": 0, "elapsed_ms": 5000}
    mock_restart = {"stdout": "", "stderr": "", "exit_code": 0, "elapsed_ms": 500}
    with patch("app.api.deploy.ShellExecutor.execute", new_callable=AsyncMock, side_effect=[mock_test, mock_restart]):
        resp = await client.post("/api/v1/deploy/self", json={"restart": True}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True


@pytest.mark.anyio
async def test_deploy_self_tests_fail(client, auth_headers):
    mock_test = {"stdout": "FAILED", "stderr": "", "exit_code": 1, "elapsed_ms": 3000}
    with patch("app.api.deploy.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_test):
        resp = await client.post("/api/v1/deploy/self", json={"restart": False}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["reason"] == "tests_failed"


@pytest.mark.anyio
async def test_project_registry_crud(client, auth_headers, tmp_path):
    with patch("app.api.deploy.PROJECTS_FILE", str(tmp_path / "registry.json")):
        # Register
        resp = await client.post(
            "/api/v1/deploy/projects/register",
            json={
                "name": "test-project",
                "path": "/tmp/test",
                "github": "test/repo",
                "stack": "python",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["registered"] == "test-project"

        # List
        resp = await client.get("/api/v1/deploy/projects", headers=auth_headers)
        assert resp.status_code == 200
        assert "test-project" in resp.json()["projects"]

        # Delete
        resp = await client.delete("/api/v1/deploy/projects/test-project", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["unregistered"] == "test-project"


@pytest.mark.anyio
async def test_project_not_found(client, auth_headers, tmp_path):
    with patch("app.api.deploy.PROJECTS_FILE", str(tmp_path / "registry.json")):
        resp = await client.get("/api/v1/deploy/projects/nonexistent", headers=auth_headers)
        assert resp.status_code == 200
        assert "error" in resp.json()


@pytest.mark.anyio
async def test_workspace_notes_crud(client, auth_headers, tmp_path):
    with patch("app.api.deploy.WORKSPACE", str(tmp_path / "workspace")):
        # Save note
        resp = await client.post(
            "/api/v1/deploy/workspace/notes",
            json={
                "name": "test.md",
                "content": "Hello World",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["saved"] == "test.md"

        # List notes
        resp = await client.get("/api/v1/deploy/workspace/notes", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()["notes"]) == 1

        # Read note
        resp = await client.get("/api/v1/deploy/workspace/notes/test.md", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["content"] == "Hello World"


@pytest.mark.anyio
async def test_workspace_note_not_found(client, auth_headers, tmp_path):
    with patch("app.api.deploy.WORKSPACE", str(tmp_path / "workspace")):
        resp = await client.get("/api/v1/deploy/workspace/notes/missing.md", headers=auth_headers)
        assert resp.status_code == 200
        assert "error" in resp.json()


@pytest.mark.anyio
async def test_memory_context_reads_db(client, tmp_path):
    """P1-a Faz-2b: memory_context get_conn + Row dict-erisim paritesi.
    get_conn row_factory olmadan r["name"] erisimi TypeError atar -> migrasyon geri alinirsa FAIL."""
    import sqlite3

    db = tmp_path / "claude_memory.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE memories (name TEXT, description TEXT, content TEXT, type TEXT, active INT, updated_at TEXT)")
    con.execute("INSERT INTO memories VALUES ('proj-x', 'aciklama', 'icerik', 'project', 1, '2026-07-01')")
    con.execute("INSERT INTO memories VALUES ('kural-1', 'geri-bildirim', 'x', 'feedback', 1, '2026-07-01')")
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, date TEXT, device_name TEXT, summary TEXT)")
    con.execute("INSERT INTO sessions (date, device_name, summary) VALUES ('2026-07-03', 'surer', 'ozet')")
    con.execute("CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, project TEXT, task TEXT, status TEXT, device_name TEXT, created_at TEXT)")
    con.execute(
        "INSERT INTO tasks_log (project, task, status, device_name, created_at) VALUES ('p', 'gorev', 'completed', 'surer', '2026-07-03')"
    )
    con.commit()
    con.close()
    with patch("app.api.deploy.read_env_var", return_value="test-internal-key"), patch("app.api.deploy.Path", lambda _p: db):
        resp = await client.get("/api/v1/deploy/memory/context", headers={"x-api-key": "test-internal-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["projects"] == [{"name": "proj-x", "description": "aciklama", "content": "icerik"}]
    assert data["recent_sessions"][0]["summary"] == "ozet"
    assert data["recent_tasks"][0]["task"] == "gorev"
    assert data["rules"] == [{"name": "kural-1", "description": "geri-bildirim"}]


@pytest.mark.anyio
async def test_memory_context_db_missing(client, tmp_path):
    """DB yok -> {"error": ...} (get_conn cagrisina gelmeden guard)."""
    with patch("app.api.deploy.read_env_var", return_value="k"), patch("app.api.deploy.Path", lambda _p: tmp_path / "yok.db"):
        resp = await client.get("/api/v1/deploy/memory/context", headers={"x-api-key": "k"})
    assert resp.status_code == 200
    assert "error" in resp.json()
