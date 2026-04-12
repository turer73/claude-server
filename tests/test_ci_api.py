"""Tests for CI/CD API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# POST /api/v1/ci/test
# ---------------------------------------------------------------------------


class TestCITestEndpoint:
    @pytest.mark.asyncio
    async def test_run_tests_success(self, client, auth_headers):
        """POST /test with valid project returns test results."""
        mock_result = {
            "project": "klipper",
            "total": 430,
            "passed": 430,
            "failed": 0,
            "duration_s": 12.5,
            "failures": [],
        }
        with patch("app.api.ci.run_project_tests", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/api/v1/ci/test",
                json={"project": "klipper"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project"] == "klipper"
        assert data["total"] == 430
        assert data["passed"] == 430
        assert data["failed"] == 0
        assert data["failures"] == []

    @pytest.mark.asyncio
    async def test_run_tests_with_failures(self, client, auth_headers):
        """POST /test returns failure details when tests fail."""
        mock_result = {
            "project": "panola",
            "total": 100,
            "passed": 98,
            "failed": 2,
            "duration_s": 5.0,
            "failures": [
                {
                    "test_file": "tests/test_auth.py",
                    "test_name": "test_login",
                    "error": "AssertionError",
                },
            ],
        }
        with patch("app.api.ci.run_project_tests", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/api/v1/ci/test",
                json={"project": "panola"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["failed"] == 2
        assert len(data["failures"]) == 1
        assert data["failures"][0]["test_name"] == "test_login"

    @pytest.mark.asyncio
    async def test_run_tests_rejects_unknown_project(self, client, auth_headers):
        """POST /test with unknown project returns 422."""
        resp = await client.post(
            "/api/v1/ci/test",
            json={"project": "nonexistent-project"},
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/ci/fix
# ---------------------------------------------------------------------------


class TestCIFixEndpoint:
    @pytest.mark.asyncio
    async def test_fix_success(self, client, auth_headers):
        """POST /fix returns fix result when Claude succeeds."""
        mock_result = {
            "fixed": True,
            "attempt": 1,
            "project": "klipper",
            "test_file": "tests/test_foo.py",
            "test_name": "test_bar",
            "claude_responses": [],
            "error": None,
        }
        with patch("app.api.ci.attempt_fix", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/api/v1/ci/fix",
                json={
                    "project": "klipper",
                    "failure": {
                        "test_file": "tests/test_foo.py",
                        "test_name": "test_bar",
                        "error": "AssertionError",
                    },
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fixed"] is True
        assert data["attempt"] == 1

    @pytest.mark.asyncio
    async def test_fix_failure(self, client, auth_headers):
        """POST /fix returns fixed=False when fix fails."""
        mock_result = {
            "fixed": False,
            "attempt": 3,
            "project": "klipper",
            "test_file": "tests/test_foo.py",
            "test_name": "test_bar",
            "claude_responses": [],
            "error": "3 deneme sonrasi duzeltilemedi",
        }
        with patch("app.api.ci.attempt_fix", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/api/v1/ci/fix",
                json={
                    "project": "klipper",
                    "failure": {
                        "test_file": "tests/test_foo.py",
                        "test_name": "test_bar",
                        "error": "AssertionError",
                    },
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fixed"] is False
        assert data["error"] is not None


# ---------------------------------------------------------------------------
# GET /api/v1/ci/status
# ---------------------------------------------------------------------------


class TestCIStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_returns_empty_initially(self, client, auth_headers):
        """GET /status returns empty status when no run has occurred."""
        with patch("app.api.ci._last_run", None):
            resp = await client.get("/api/v1/ci/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_run"] == "never"
        assert data["total_tests"] == 0
        assert data["passed"] == 0
        assert data["failed"] == 0
        assert data["projects"] == []


# ---------------------------------------------------------------------------
# Auth requirement tests
# ---------------------------------------------------------------------------


class TestCIAuthRequired:
    @pytest.mark.asyncio
    async def test_test_requires_auth(self, client):
        """POST /test without auth returns 401."""
        resp = await client.post(
            "/api/v1/ci/test",
            json={"project": "klipper"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_fix_requires_auth(self, client):
        """POST /fix without auth returns 401."""
        resp = await client.post(
            "/api/v1/ci/fix",
            json={
                "project": "klipper",
                "failure": {
                    "test_file": "tests/test_foo.py",
                    "test_name": "test_bar",
                    "error": "AssertionError",
                },
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_status_requires_auth(self, client):
        """GET /status without auth returns 401."""
        resp = await client.get("/api/v1/ci/status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_run_all_requires_auth(self, client):
        """POST /run-all without auth returns 401."""
        resp = await client.post("/api/v1/ci/run-all")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_read_only_cannot_access_test(self, client, read_headers):
        """POST /test with read-only token returns 403."""
        resp = await client.post(
            "/api/v1/ci/test",
            json={"project": "klipper"},
            headers=read_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_read_only_cannot_access_status(self, client, read_headers):
        """GET /status with read-only token returns 403."""
        resp = await client.get("/api/v1/ci/status", headers=read_headers)
        assert resp.status_code == 403
