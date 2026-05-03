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

    # baseline=20 * 1.5 = 30, so 52% is anomalous. Must also be >= threshold*0.6 (85*0.6=51)
    metrics = {"cpu_percent": 52, "memory_percent": 30, "disk_percent": 40, "temperature": 35}
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


# ── Store Metrics Tests ────────────────────────


async def test_store_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "devops.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    metrics = {
        "timestamp": "2026-03-29T14:00:00Z",
        "cpu_percent": 45,
        "memory_percent": 60,
        "disk_percent": 34,
        "temperature": 55,
        "load_avg": [1.0, 0.8, 0.7],
        "network_sent_mb": 100,
        "network_recv_mb": 200,
    }
    await agent._store_metrics(metrics)
    rows = await db.fetch_all("SELECT * FROM metrics_history")
    assert len(rows) == 1
    assert rows[0]["cpu_usage"] == 45
    await db.close()
    get_settings.cache_clear()


async def test_store_metrics_no_db():
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    await agent._store_metrics({"cpu_percent": 50})  # Should not raise


# ── Store Alert Tests ──────────────────────────


async def test_store_alert(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import Alert, DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "devops2.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    alert = Alert(
        id="cpu-1", severity="critical", source="cpu", message="CPU at 95%", value=95, threshold=85, timestamp="2026-03-29T14:00:00Z"
    )
    await agent._store_alert(alert)
    rows = await db.fetch_all("SELECT * FROM alerts")
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    await db.close()
    get_settings.cache_clear()


async def test_store_alert_no_db():
    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    alert = Alert(id="x", severity="warning", source="cpu", message="test", value=80, threshold=85, timestamp="now")
    await agent._store_alert(alert)  # Should not raise


# ── Remediation Tests ──────────────────────────


async def test_remediate_cpu_critical():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    alert = Alert(id="cpu-1", severity="critical", source="cpu", message="CPU at 95%", value=95, threshold=85, timestamp="now")

    mock_result = {"stdout": "done\n", "stderr": "", "exit_code": 0}
    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, return_value=mock_result),
        patch.object(agent, "_send_webhook", new_callable=AsyncMock),
    ):
        await agent._remediate(alert)

    assert len(agent._remediation_log) > 0
    assert agent._remediation_log[0].alert_source == "cpu"
    assert agent._remediation_log[0].success is True


async def test_remediate_cooldown():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    alert = Alert(id="cpu-1", severity="critical", source="cpu", message="CPU high", value=95, threshold=85, timestamp="now")

    mock_result = {"stdout": "ok\n", "stderr": "", "exit_code": 0}
    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, return_value=mock_result),
        patch.object(agent, "_send_webhook", new_callable=AsyncMock),
    ):
        await agent._remediate(alert)
        count_1 = len(agent._remediation_log)

        # Second remediation within cooldown — should be skipped
        await agent._remediate(alert)
        count_2 = len(agent._remediation_log)
        assert count_1 == count_2  # No new remediation


async def test_remediate_no_playbook():
    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    alert = Alert(id="x-1", severity="critical", source="nonexistent", message="test", value=99, threshold=50, timestamp="now")
    await agent._remediate(alert)
    assert len(agent._remediation_log) == 0


async def test_remediate_exception():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    alert = Alert(id="cpu-1", severity="critical", source="cpu", message="CPU high", value=95, threshold=85, timestamp="now")

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=RuntimeError("fail")),
        patch.object(agent, "_send_webhook", new_callable=AsyncMock),
    ):
        await agent._remediate(alert)

    assert len(agent._remediation_log) > 0
    assert agent._remediation_log[0].success is False


# ── Check Services Tests ───────────────────────


async def test_check_services_all_active():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)

    async def mock_exec(cmd, timeout=5):
        if "systemctl is-active" in cmd:
            return {"stdout": "active\n", "stderr": "", "exit_code": 0}
        if "docker ps" in cmd:
            return {"stdout": "Up 10 days\n", "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
        await agent._check_services()

    assert len(agent._active_alerts) == 0


async def test_check_services_service_down():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)

    async def mock_exec(cmd, timeout=5):
        if "systemctl is-active" in cmd:
            return {"stdout": "inactive\n", "stderr": "", "exit_code": 3}
        if "docker ps" in cmd:
            return {"stdout": "Up 10 days\n", "stderr": "", "exit_code": 0}
        if "systemctl restart" in cmd:
            return {"stdout": "", "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
        await agent._check_services()

    # Should have alerts for down services
    service_alerts = [k for k in agent._active_alerts if k.startswith("service:")]
    assert len(service_alerts) > 0


# ── Resolve Alert DB Tests ─────────────────────


async def test_resolve_alert_db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import Alert, DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "resolve.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)

    # Store then resolve
    alert = Alert(
        id="cpu-1",
        severity="critical",
        source="cpu",
        message="CPU high",
        value=95,
        threshold=85,
        timestamp="now",
        resolved=True,
        resolved_at="later",
    )
    await agent._store_alert(alert)
    await agent._resolve_alert_db(alert)

    rows = await db.fetch_all("SELECT * FROM alerts WHERE source = 'cpu'")
    assert rows[0]["resolved"] == 1
    await db.close()
    get_settings.cache_clear()


# ── Start / Stop Tests ─────────────────────────


async def test_start_stop():
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent.start()
    assert agent._running is True
    agent.start()  # no-op
    await agent.stop()
    assert agent._running is False


# ── Alert Dataclass Tests ──────────────────────


def test_alert_defaults():
    from app.core.devops_agent import Alert

    a = Alert(id="x", severity="warning", source="cpu", message="test", value=80, threshold=85, timestamp="now")
    assert a.resolved is False
    assert a.resolved_at is None
    assert a.remediation is None


def test_remediation_record():
    from app.core.devops_agent import RemediationRecord

    r = RemediationRecord(timestamp="now", alert_source="cpu", action="log", command="ps aux", result="ok", success=True)
    assert r.success is True
