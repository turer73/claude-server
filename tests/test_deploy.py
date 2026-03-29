"""Tests for Deploy API — self-deploy, project registry, workspace notes."""

import pytest
from unittest.mock import patch, AsyncMock

from tests.conftest import TEST_API_KEY


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
        resp = await client.post("/api/v1/deploy/projects/register", json={
            "name": "test-project", "path": "/tmp/test", "github": "test/repo", "stack": "python",
        }, headers=auth_headers)
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
        resp = await client.post("/api/v1/deploy/workspace/notes", json={
            "name": "test.md", "content": "Hello World",
        }, headers=auth_headers)
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
