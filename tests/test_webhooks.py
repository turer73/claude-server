"""Tests for webhook endpoints (n8n integration)."""

import pytest


@pytest.mark.anyio
async def test_receive_webhook(client):
    resp = await client.post("/api/v1/monitor/webhooks/receive", json={
        "source": "n8n",
        "event": "test_event",
        "data": {"key": "value"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert "event_id" in data
    assert "timestamp" in data


@pytest.mark.anyio
async def test_receive_webhook_no_data(client):
    resp = await client.post("/api/v1/monitor/webhooks/receive", json={
        "source": "external",
        "event": "ping",
    })
    assert resp.status_code == 200
    assert resp.json()["received"] is True


@pytest.mark.anyio
async def test_list_events(client):
    # Send a webhook first
    await client.post("/api/v1/monitor/webhooks/receive", json={
        "source": "test",
        "event": "ping",
    })
    resp = await client.get("/api/v1/monitor/webhooks/events")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) >= 1


@pytest.mark.anyio
async def test_list_events_with_limit(client):
    # Send multiple webhooks
    for i in range(5):
        await client.post("/api/v1/monitor/webhooks/receive", json={
            "source": "test",
            "event": f"event_{i}",
        })
    resp = await client.get("/api/v1/monitor/webhooks/events?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) <= 2


@pytest.mark.anyio
async def test_trigger_health_check(client):
    resp = await client.post("/api/v1/monitor/webhooks/trigger/health_check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "health_check"
    assert "healthy" in data
    assert "metrics" in data


@pytest.mark.anyio
async def test_trigger_metrics_snapshot(client):
    resp = await client.post("/api/v1/monitor/webhooks/trigger/metrics_snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "metrics_snapshot"
    assert "metrics" in data
    assert "cpu_percent" in data["metrics"]


@pytest.mark.anyio
async def test_trigger_alert_check(client):
    resp = await client.post("/api/v1/monitor/webhooks/trigger/alert_check", json={
        "thresholds": {
            "cpu_percent": 85,
            "memory_percent": 85,
            "disk_percent": 90,
            "temperature_c": 80,
        }
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "alert_check"
    assert "has_alerts" in data
    assert "alerts" in data


@pytest.mark.anyio
async def test_trigger_alert_check_default_thresholds(client):
    resp = await client.post("/api/v1/monitor/webhooks/trigger/alert_check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "alert_check"
    assert "has_alerts" in data


@pytest.mark.anyio
async def test_trigger_unknown_action(client):
    resp = await client.post("/api/v1/monitor/webhooks/trigger/nonexistent")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert "available" in data
    assert "health_check" in data["available"]
