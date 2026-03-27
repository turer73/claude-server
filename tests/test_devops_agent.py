"""Tests for the Autonomous DevOps Agent."""

import pytest


@pytest.fixture
async def devops_client(client, app):
    """Client with DevOps agent initialized."""
    from app.core.devops_agent import DevOpsAgent
    db = app.state.db
    agent = DevOpsAgent(db=db, interval=60)
    app.state.devops_agent = agent
    yield client
    await agent.stop()


# ── API Endpoint Tests ──────────────────────────


async def test_devops_status(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "thresholds" in data
    assert data["check_count"] == 0


async def test_devops_status_no_auth(devops_client):
    resp = await devops_client.get("/api/v1/devops/status")
    assert resp.status_code in (401, 403)


async def test_devops_active_alerts(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/alerts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["alerts"] == []


async def test_devops_alerts_history(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/alerts/history", headers=auth_headers)
    assert resp.status_code == 200
    assert "count" in resp.json()


async def test_devops_alerts_history_filter(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/alerts/history?severity=critical&limit=10", headers=auth_headers)
    assert resp.status_code == 200


async def test_devops_metrics_history(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/metrics/history?minutes=5", headers=auth_headers)
    assert resp.status_code == 200
    assert "metrics" in resp.json()


async def test_devops_metrics_buffer(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/metrics/buffer", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0  # No ticks yet


async def test_devops_remediation_log(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/remediation/log", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["remediations"] == []


async def test_devops_playbooks(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/playbooks", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "cpu_critical" in data["playbooks"]
    assert "memory_critical" in data["playbooks"]
    assert "disk_critical" in data["playbooks"]
    assert "critical_services" in data
    assert "critical_containers" in data


# ── Unit Tests ──────────────────────────────────


async def test_detect_normal_no_alerts():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 20, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    alerts = agent._detect(metrics)
    assert len(alerts) == 0


async def test_detect_cpu_critical():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 95, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    alerts = agent._detect(metrics)
    assert len(alerts) == 1
    assert alerts[0].source == "cpu"
    assert alerts[0].severity == "critical"


async def test_detect_warning_zone():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)
    # 85 * 0.9 = 76.5 — so 80% is warning territory
    metrics = {"cpu_percent": 80, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    alerts = agent._detect(metrics)
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"


async def test_detect_multiple_alerts():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 95, "memory_percent": 92, "disk_percent": 95, "temperature": 85}
    alerts = agent._detect(metrics)
    assert len(alerts) == 4


async def test_auto_resolve():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)

    # Trigger
    agent._detect({"cpu_percent": 95, "memory_percent": 40, "disk_percent": 50, "temperature": 45})
    assert "cpu" in agent._active_alerts

    # Resolve: 85 * 0.85 = 72.25, so 30% is well below
    agent._auto_resolve({"cpu_percent": 30, "memory_percent": 40, "disk_percent": 50, "temperature": 45})
    assert "cpu" not in agent._active_alerts


async def test_no_duplicate_alerts():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)

    # First detection
    alerts1 = agent._detect({"cpu_percent": 95, "memory_percent": 40, "disk_percent": 50, "temperature": 45})
    assert len(alerts1) == 1

    # Second detection — same source already active, no new alert
    alerts2 = agent._detect({"cpu_percent": 96, "memory_percent": 40, "disk_percent": 50, "temperature": 45})
    assert len(alerts2) == 0


async def test_baseline_calculation():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)

    # Not enough data
    assert agent._baseline("cpu_percent") is None

    # Fill 20 samples
    for _ in range(20):
        agent._history.append({"cpu_percent": 25})

    assert agent._baseline("cpu_percent") == 25.0


async def test_baseline_anomaly_detection():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=60)

    # Fill baseline at 20%
    for _ in range(20):
        agent._history.append({"cpu_percent": 20, "memory_percent": 30, "disk_percent": 40, "temperature": 35})

    # 20 * 1.5 = 30, so 50% is anomalous but below static threshold (85%)
    metrics = {"cpu_percent": 50, "memory_percent": 30, "disk_percent": 40, "temperature": 35}
    alerts = agent._detect(metrics)
    assert len(alerts) == 1
    assert alerts[0].source == "cpu"
    assert alerts[0].severity == "warning"


async def test_status_property():
    from app.core.devops_agent import DevOpsAgent
    agent = DevOpsAgent(db=None, interval=30)
    s = agent.status
    assert s["running"] is False
    assert s["check_count"] == 0
    assert s["interval_seconds"] == 30


async def test_playbooks_defined():
    from app.core.devops_agent import PLAYBOOKS
    assert "cpu_critical" in PLAYBOOKS
    assert "memory_critical" in PLAYBOOKS
    assert "disk_critical" in PLAYBOOKS
    assert "temperature_critical" in PLAYBOOKS
    assert "service_down" in PLAYBOOKS
    assert "docker_down" in PLAYBOOKS
