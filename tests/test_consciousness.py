"""Tests for ConsciousnessStream (Functionalism Faz 1)."""

from __future__ import annotations

import pytest

from app.core.consciousness import _build_content, _determine_emotion, _determine_focus

# ── Helpers ──────────────────────────────────────


def _state(**overrides: dict) -> dict:
    """Default empty state override pattern."""
    base = {
        "alerts": {"critical_count": 0, "warning_count": 0, "critical_sources": []},
        "events": {"total": 0, "critical": 0},
        "cron_outcomes": {"partial_count": 0, "fail_count": 0, "partial_jobs": [], "fail_jobs": []},
        "metrics": {},
        "spawn_status": {"poison_count": 0, "pending_count": 0},
        "notes": {"unread": 0},
        "llm": {"total": 0},
    }
    base.update(overrides)
    return base


# ── _determine_focus ─────────────────────────────


class TestDetermineFocus:
    def test_idle(self):
        assert _determine_focus(_state()) == "idle"

    def test_alert_critical(self):
        s = _state(alerts={"critical_count": 2, "warning_count": 0, "critical_sources": ["sys"]})
        assert _determine_focus(s) == "alert:sys"

    def test_alert_critical_no_sources(self):
        s = _state(alerts={"critical_count": 1, "warning_count": 0, "critical_sources": []})
        assert _determine_focus(s) == "alert:critical"

    def test_cron_fail(self):
        s = _state(cron_outcomes={"fail_count": 1, "partial_count": 0, "partial_jobs": [], "fail_jobs": ["backup"]})
        assert _determine_focus(s) == "cron:fail"

    def test_cron_partial(self):
        s = _state(cron_outcomes={"partial_count": 2, "fail_count": 0, "partial_jobs": ["health"], "fail_jobs": []})
        assert _determine_focus(s) == "cron:partial"

    def test_spawn_poison(self):
        s = _state(spawn_status={"poison_count": 1, "pending_count": 0})
        assert _determine_focus(s) == "spawn:poison"

    def test_spawn_pending(self):
        s = _state(spawn_status={"poison_count": 0, "pending_count": 3})
        assert _determine_focus(s) == "spawn:pending"

    def test_metric_cpu(self):
        s = _state(metrics={"cpu": 85})
        assert _determine_focus(s) == "metric:cpu"

    def test_metric_memory(self):
        s = _state(metrics={"cpu": 50, "memory": 90})
        assert _determine_focus(s) == "metric:memory"

    def test_event_critical(self):
        s = _state(events={"total": 5, "critical": 1})
        assert _determine_focus(s) == "event:recent"

    def test_priority_alert_wins(self):
        s = _state(
            alerts={"critical_count": 1, "warning_count": 0, "critical_sources": ["fw"]},
            spawn_status={"poison_count": 3, "pending_count": 0},
        )
        assert _determine_focus(s) == "alert:fw"


# ── _determine_emotion ───────────────────────────


class TestDetermineEmotion:
    def test_calm(self):
        assert _determine_emotion(_state()) == "calm"

    def test_concerned_crit(self):
        s = _state(alerts={"critical_count": 1, "warning_count": 0, "critical_sources": []})
        assert _determine_emotion(s) == "concerned"

    def test_concerned_fail(self):
        s = _state(cron_outcomes={"fail_count": 2, "partial_count": 0, "partial_jobs": [], "fail_jobs": ["x"]})
        assert _determine_emotion(s) == "concerned"

    def test_restless_partial_3(self):
        s = _state(cron_outcomes={"partial_count": 3, "fail_count": 0, "partial_jobs": ["a", "b", "c"], "fail_jobs": []})
        assert _determine_emotion(s) == "restless"

    def test_restless_poison(self):
        s = _state(spawn_status={"poison_count": 2, "pending_count": 0})
        assert _determine_emotion(s) == "restless"

    def test_busy_cpu(self):
        s = _state(metrics={"cpu": 90})
        assert _determine_emotion(s) == "busy"

    def test_busy_events(self):
        s = _state(events={"total": 15, "critical": 0})
        assert _determine_emotion(s) == "busy"


# ── _build_content ───────────────────────────────


class TestBuildContent:
    def test_empty(self):
        assert _build_content(_state(), "idle") == "her sey sakin"

    def test_with_alerts(self):
        s = _state(alerts={"critical_count": 2, "warning_count": 1, "critical_sources": ["fw", "db"]})
        content = _build_content(s, "alert:fw")
        assert "2 kritik" in content
        assert "1 uyari" in content

    def test_with_cron_partial(self):
        s = _state(cron_outcomes={"partial_count": 2, "fail_count": 0, "partial_jobs": ["health"], "fail_jobs": []})
        content = _build_content(s, "cron:partial")
        assert "cron partial" in content

    def test_with_metrics(self):
        s = _state(metrics={"cpu": 75.3})
        content = _build_content(s, "idle")
        assert "CPU %75" in content or "CPU" in content

    def test_with_notes(self):
        s = _state(notes={"unread": 5})
        content = _build_content(s, "idle")
        assert "5 okunmamis" in content

    def test_with_llm(self):
        s = _state(llm={"total": 8})
        content = _build_content(s, "idle")
        assert "LLM cagrisi" in content

    def test_with_poison(self):
        s = _state(spawn_status={"poison_count": 1, "pending_count": 2})
        content = _build_content(s, "spawn:poison")
        assert "poison" in content
        assert "retry" in content


# ── API endpoint tests ───────────────────────────


@pytest.fixture
async def consciousness_client(client, app):
    """Client with ConsciousnessStream initialized (not running)."""
    from app.core.consciousness import ConsciousnessStream

    stream = ConsciousnessStream(interval=300)
    app.state.consciousness_stream = stream
    stream.start()
    yield client
    await stream.stop()


class TestApiStatus:
    async def test_status(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["thought_count"] >= 0
        assert "emotion" in data

    async def test_status_no_auth(self, consciousness_client):
        resp = await consciousness_client.get("/api/v1/consciousness/status")
        assert resp.status_code in (401, 403)

    async def test_stream(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/stream", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "thoughts" in data
        assert "count" in data

    async def test_stream_with_limit(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/stream?limit=5", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] <= 5

    async def test_self_model(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/self", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "emotion" in data
        assert "focus" in data
        assert "state" in data

    async def test_self_no_auth(self, consciousness_client):
        resp = await consciousness_client.get("/api/v1/consciousness/self")
        assert resp.status_code in (401, 403)


class TestApiNoStream:
    """API responses when stream is not initialized."""

    async def test_status_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    async def test_stream_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/stream", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["error"] == "not_initialized"

    async def test_self_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/self", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["error"] == "not_initialized"
