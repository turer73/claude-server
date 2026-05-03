"""Tests for VPS Bridge API."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_vps_exec_requires_admin(client, read_headers):
    resp = await client.post("/api/v1/vps/exec", json={"command": "hostname"}, headers=read_headers)
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_vps_exec_with_admin(client, auth_headers):
    mock_result = {"stdout": "vps-host\n", "stderr": "", "exit_code": 0, "elapsed_ms": 120}
    with patch("app.api.vps.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post("/api/v1/vps/exec", json={"command": "hostname"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stdout"] == "vps-host\n"
        assert data["exit_code"] == 0


@pytest.mark.anyio
async def test_vps_status_online(client, auth_headers):
    mock_result = {
        "stdout": "HOSTNAME=vps\nUPTIME=up 10 days\nCPU=4\nRAM_USED=2.8Gi/7.8Gi\nDISK=13G/145G (9%)\nCONTAINER=coolify:Up 10 days\n",
        "stderr": "",
        "exit_code": 0,
        "elapsed_ms": 500,
    }
    with patch("app.api.vps.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.get("/api/v1/vps/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["online"] is True
        assert data["hostname"] == "vps"
        assert len(data["containers"]) == 1


@pytest.mark.anyio
async def test_vps_status_offline(client, auth_headers):
    mock_result = {"stdout": "", "stderr": "Connection refused", "exit_code": 255, "elapsed_ms": 100}
    with patch("app.api.vps.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.get("/api/v1/vps/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["online"] is False


@pytest.mark.anyio
async def test_vps_services(client, auth_headers):
    mock_result = {"stdout": "coolify=200\nuptime=200\nn8n=200\nanalytics=200\n", "stderr": "", "exit_code": 0, "elapsed_ms": 300}
    with patch("app.api.vps.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.get("/api/v1/vps/services", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data


@pytest.mark.anyio
async def test_vps_deploy_unknown_project(client, auth_headers):
    mock_result = {"stdout": "", "stderr": "", "exit_code": 0, "elapsed_ms": 10}
    with patch("app.api.vps.ShellExecutor.execute", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post("/api/v1/vps/deploy/nonexistent", headers=auth_headers)
        assert resp.status_code == 200
        assert "error" in resp.json()
