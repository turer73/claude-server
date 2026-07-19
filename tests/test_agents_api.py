"""Tests for agent management API endpoints."""

from __future__ import annotations

import pytest


class TestListAgents:
    async def test_list_agents(self, client, auth_headers):
        resp = await client.get("/api/v1/agents/list", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data

    async def test_list_agents_no_auth(self, client):
        resp = await client.get("/api/v1/agents/list")
        assert resp.status_code in (401, 403)

    async def test_list_agents_readonly(self, client, read_headers):
        resp = await client.get("/api/v1/agents/list", headers=read_headers)
        assert resp.status_code == 200


class TestBusStatus:
    async def test_bus_status(self, client, auth_headers):
        resp = await client.get("/api/v1/agents/bus", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "bus" in data
        assert "recent_events" in data
        assert data["worker"]["continuous_agents_role"] == "unknown"

    async def test_bus_status_no_auth(self, client):
        resp = await client.get("/api/v1/agents/bus")
        assert resp.status_code in (401, 403)

    async def test_bus_status_with_app_state(self, app, client, auth_headers):
        from app.core.agent_bus import AgentBus

        bus = AgentBus()
        bus.register_agent("test_agent")
        app.state.agent_bus = bus
        resp = await client.get("/api/v1/agents/bus", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bus"]["agents"]) == 1


class TestRuntimeAgents:
    async def test_runtime_endpoint(self, client, auth_headers):
        resp = await client.get("/api/v1/agents/runtime", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data

    async def test_runtime_reports_local_continuous_agent_role(self, app, client, auth_headers):
        app.state.continuous_agents_role = "standby"
        resp = await client.get("/api/v1/agents/runtime", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["worker"]["continuous_agents_role"] == "standby"
        assert isinstance(resp.json()["worker"]["pid"], int)

    async def test_runtime_sanitizes_continuous_agent_error(self, app, client, auth_headers):
        app.state.continuous_agents_lock_error = "lock failed\n" + ("x" * 300)

        resp = await client.get("/api/v1/agents/runtime", headers=auth_headers)

        error = resp.json()["worker"]["continuous_agents_error"]
        assert "\n" not in error
        assert len(error) == 240

    async def test_runtime_no_auth(self, client):
        resp = await client.get("/api/v1/agents/runtime")
        assert resp.status_code in (401, 403)


class TestCreateAgent:
    async def test_create_agent(self, client, auth_headers):
        payload = {
            "name": "test-agent",
            "description": "A test agent",
            "trigger": "manual",
            "tools": ["shell_exec"],
        }
        resp = await client.post("/api/v1/agents/create", headers=auth_headers, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] is True

    async def test_create_agent_no_auth(self, client):
        resp = await client.post("/api/v1/agents/create", json={"name": "x", "description": "x", "trigger": "manual", "tools": []})
        assert resp.status_code in (401, 403)

    async def test_create_and_get_agent(self, client, auth_headers):
        payload = {
            "name": "custom-agent",
            "description": "Custom test agent",
            "trigger": "cron",
            "schedule": "*/5 * * * *",
            "tools": ["file_read", "shell_exec"],
            "system_prompt": "You are a test agent.",
            "steps": [{"tool": "shell_exec", "params": {"command": "echo hello"}}],
        }
        await client.post("/api/v1/agents/create", headers=auth_headers, json=payload)
        resp = await client.get("/api/v1/agents/custom-agent", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "custom-agent"

    async def test_get_agent_not_found(self, client, auth_headers):
        resp = await client.get("/api/v1/agents/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestDeleteAgent:
    async def test_create_and_delete(self, client, auth_headers):
        payload = {
            "name": "temp-agent",
            "description": "Will be deleted",
            "trigger": "manual",
            "tools": [],
        }
        await client.post("/api/v1/agents/create", headers=auth_headers, json=payload)
        resp = await client.delete("/api/v1/agents/temp-agent", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    async def test_delete_no_auth(self, client, auth_headers):
        payload = {
            "name": "delete-me",
            "description": "x",
            "trigger": "manual",
            "tools": [],
        }
        await client.post("/api/v1/agents/create", headers=auth_headers, json=payload)
        resp = await client.delete("/api/v1/agents/delete-me")
        assert resp.status_code in (401, 403)


class TestTriggerAgent:
    async def test_trigger_unknown(self, client, auth_headers):
        resp = await client.post("/api/v1/agents/runtime/unknown/trigger", headers=auth_headers)
        assert resp.status_code == 404

    async def test_trigger_no_auth(self, client):
        resp = await client.post("/api/v1/agents/runtime/devops/trigger")
        assert resp.status_code in (401, 403)

    async def test_code_review_trigger_queues_from_standby_worker(self, app, client, auth_headers):
        calls = []

        class FakeCodeReview:
            def status(self) -> dict:
                return {"enabled": True, "running": False}

            def request_manual_run(self) -> str:
                calls.append("request")
                return "queued"

        app.state.code_review_agent = FakeCodeReview()
        app.state.continuous_agents_role = "standby"

        resp = await client.post("/api/v1/agents/runtime/code-review/trigger", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["queued"] is True
        assert calls == ["request"]

    async def test_code_review_trigger_persists_request(self, app, client, auth_headers):
        completed = []

        class FakeCodeReview:
            def status(self) -> dict:
                return {"enabled": True, "running": True}

            def request_manual_run(self) -> str:
                completed.append(True)
                return "queued"

        app.state.code_review_agent = FakeCodeReview()
        app.state.continuous_agents_role = "leader"
        app.state.continuous_agents_started = True

        resp = await client.post("/api/v1/agents/runtime/code-review/trigger", headers=auth_headers)

        assert resp.status_code == 200
        assert completed == [True]

    async def test_code_review_trigger_rejects_disabled_agent(self, app, client, auth_headers):
        class FakeCodeReview:
            def status(self) -> dict:
                return {"enabled": False, "running": False}

        app.state.code_review_agent = FakeCodeReview()
        app.state.continuous_agents_role = "leader"
        app.state.continuous_agents_started = True

        resp = await client.post("/api/v1/agents/runtime/code-review/trigger", headers=auth_headers)

        assert resp.status_code == 409
        assert "disabled" in resp.json()["detail"]

    async def test_code_review_trigger_coalesces_manual_requests(self, app, client, auth_headers):
        class FakeCodeReview:
            def status(self) -> dict:
                return {"enabled": True, "running": True}

            def request_manual_run(self) -> str:
                return "already_queued"

        app.state.code_review_agent = FakeCodeReview()
        app.state.continuous_agents_role = "leader"
        app.state.continuous_agents_started = True

        resp = await client.post("/api/v1/agents/runtime/code-review/trigger", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["queued"] is False
        assert resp.json()["coalesced"] is True

    async def test_code_review_trigger_reports_persistence_failure(self, app, client, auth_headers):
        class FakeCodeReview:
            def status(self) -> dict:
                return {"enabled": True, "running": False}

            def request_manual_run(self) -> str:
                return "error"

        app.state.code_review_agent = FakeCodeReview()

        resp = await client.post("/api/v1/agents/runtime/code-review/trigger", headers=auth_headers)

        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"

    async def test_trigger_rejects_triggerable_false_agents(self, client, auth_headers):
        # Codex #328-P2 (security): ci-fix-runall require_admin-korumalı /ci/run-all'ı çağırır,
        # self-pentest 600s generic-timeout'tan uzun sürer — ikisi de triggerable=False. _CRON_SCRIPTS
        # bu anahtarları hiç içermez (bkz app/api/agents.py), yani ADMIN-token bile (auth_headers)
        # generic trigger'dan onlara ulaşamaz — sadece UI-buton gizleme değil, sunucu-taraflı gate.
        for key in ("ci-fix-runall", "self-pentest"):
            resp = await client.post(f"/api/v1/agents/runtime/{key}/trigger", headers=auth_headers)
            assert resp.status_code == 404, f"{key} tetiklenebiliyor — triggerable=False gate delinmiş"


class TestSelfImprovement:
    async def test_pending_no_auth(self, client):
        resp = await client.get("/api/v1/agents/self-improvement/pending")
        assert resp.status_code in (401, 403)

    async def test_pending_empty(self, client, auth_headers):
        resp = await client.get("/api/v1/agents/self-improvement/pending", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "suggestions" in data

    @pytest.mark.anyio
    async def test_approve_missing_id(self, client, auth_headers):
        resp = await client.post("/api/v1/agents/self-improvement/approve", headers=auth_headers, json={})
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_approve_not_found(self, client, auth_headers):
        resp = await client.post("/api/v1/agents/self-improvement/approve", headers=auth_headers, json={"id": 999})
        # Test ortamında DB yoksa 500 (DB bağlantı hatası), DB varsa 404
        assert resp.status_code in (404, 500)

    @pytest.mark.anyio
    async def test_approve_no_auth(self, client):
        resp = await client.post("/api/v1/agents/self-improvement/approve", json={"id": 1})
        assert resp.status_code in (401, 403)
