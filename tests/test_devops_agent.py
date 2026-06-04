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


async def test_store_alert_bridges_to_events(tmp_path, monkeypatch):
    # LIVESYS Faz 3.2 alerts-bridge: _store_alert alerts-INSERT'in YANINDA merkezi
    # events'e de emit_event yazar (TEK-writer). warning->warn normalize; alerts-INSERT
    # korunur. KAYIT-ONLY (notify yok) -> double-notify yaratmaz.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import Alert, DevOpsAgent
    from app.db.database import Database

    dbpath = str(tmp_path / "bridge.db")
    monkeypatch.setenv("DB_PATH", dbpath)  # emit_event AYNI db'ye yazsin (alerts+events tek dosya)
    db = Database(dbpath)
    await db.initialize()  # SCHEMA_V1 -> alerts + events tablolari
    agent = DevOpsAgent(db=db, interval=60)
    alert = Alert(
        id="cpu-1", severity="warning", source="cpu", message="CPU at 88%", value=88, threshold=85, timestamp="2026-06-03T06:00:00Z"
    )
    await agent._store_alert(alert)

    alerts = await db.fetch_all("SELECT severity, source FROM alerts")
    assert alerts == [{"severity": "warning", "source": "cpu"}]  # alerts-INSERT korundu

    events = await db.fetch_all("SELECT type, source, severity, title, notified FROM events")
    assert len(events) == 1  # bridge: events'e de yazildi
    assert events[0]["type"] == "alert"
    assert events[0]["source"] == "cpu"
    assert events[0]["severity"] == "warn"  # warning -> warn normalize
    assert events[0]["title"] == "CPU at 88%"
    assert events[0]["notified"] == 0  # KAYIT-ONLY (notify-cron sonra drain eder)
    await db.close()
    get_settings.cache_clear()


async def test_store_alert_bridge_emit_failure_is_safe(tmp_path, monkeypatch):
    # emit_event beklenmedik şekilde raise etse bile _store_alert bozulmamalı
    # (best-effort guard); alerts-INSERT yine de gerçekleşmiş olmalı.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import Alert, DevOpsAgent
    from app.db.database import Database

    dbpath = str(tmp_path / "bridge_fail.db")
    monkeypatch.setenv("DB_PATH", dbpath)
    db = Database(dbpath)
    await db.initialize()

    def _boom(*a, **k):
        raise RuntimeError("emit blew up")

    monkeypatch.setattr("app.core.devops_agent.emit_event", _boom)

    agent = DevOpsAgent(db=db, interval=60)
    alert = Alert(
        id="cpu-1", severity="critical", source="cpu", message="CPU 99%", value=99, threshold=85, timestamp="2026-06-03T06:00:00Z"
    )
    await agent._store_alert(alert)  # emit raise -> PROPAGATE ETMEMELI

    alerts = await db.fetch_all("SELECT severity FROM alerts")
    assert len(alerts) == 1  # alerts-INSERT emit-fail'den ETKILENMEDI
    await db.close()
    get_settings.cache_clear()


# ── Remediation Tests ──────────────────────────


async def test_remediate_cpu_critical():
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import Alert, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent._remediation_mode = "auto"  # FAZ5: exec yolunu test et (default 'notify' artık yürütmez)
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


# ── VPS Metrics Tests ──────────────────────────


def test_parse_vps_probe():
    from app.core.devops_agent import parse_vps_probe

    out = "CPU=32.5\nMEM=35.6\nDISK=20\nCTOTAL=20\nCUP=18\nNAMES=traefik,postgres,n8n,\n"
    p = parse_vps_probe(out)
    assert p["cpu"] == 32.5
    assert p["mem"] == 35.6
    assert p["disk"] == 20.0
    assert p["containers_total"] == 20
    assert p["containers_up"] == 18
    assert p["names"] == ["traefik", "postgres", "n8n"]


def test_parse_vps_probe_partial():
    from app.core.devops_agent import parse_vps_probe

    # Garbage / partial output → None fields, empty names (caller treats cpu=None as failure)
    p = parse_vps_probe("error: connection refused\n")
    assert p["cpu"] is None
    assert p["names"] == []


async def test_store_vps_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    await agent._store_vps_metrics(
        {"cpu": 30.0, "mem": 40.0, "disk": 20.0, "containers_total": 20, "containers_up": 20},
        online=True,
    )
    rows = await db.fetch_all("SELECT * FROM vps_metrics_history")
    assert len(rows) == 1
    assert rows[0]["cpu_usage"] == 30.0
    assert rows[0]["online"] == 1
    assert rows[0]["containers_up"] == 20
    await db.close()
    get_settings.cache_clear()


async def test_store_vps_metrics_no_db():
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    await agent._store_vps_metrics({"cpu": 1.0}, online=True)  # Should not raise


async def test_check_vps_reachable(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps2.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    agent._vps_containers = ["traefik", "missing-one"]

    probe = {
        "cpu": 25.0,
        "mem": 50.0,
        "disk": 20.0,
        "containers_total": 20,
        "containers_up": 19,
        "names": ["traefik", "postgres"],
    }
    with patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=probe):
        await agent._check_vps()

    # Persisted + latest cached
    rows = await db.fetch_all("SELECT * FROM vps_metrics_history")
    assert len(rows) == 1
    assert rows[0]["online"] == 1
    assert agent.latest_vps["online"] is True
    # missing-one is not in running set → warning; traefik is fine
    assert "vps:missing-one" in agent._active_alerts
    assert "vps:traefik" not in agent._active_alerts
    assert "vps:offline" not in agent._active_alerts
    await db.close()
    get_settings.cache_clear()


async def test_check_vps_offline(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps3.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)

    with patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=None):
        await agent._check_vps()

    rows = await db.fetch_all("SELECT * FROM vps_metrics_history")
    assert len(rows) == 1
    assert rows[0]["online"] == 0
    assert "vps:offline" in agent._active_alerts
    assert agent.latest_vps["online"] is False
    await db.close()
    get_settings.cache_clear()


async def test_vps_ssh_probe_no_host():
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent._vps_host = ""
    assert await agent._vps_ssh_probe() is None


async def test_devops_vps_metrics_history_route(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/vps/metrics/history?minutes=60", headers=auth_headers)
    assert resp.status_code == 200
    assert "metrics" in resp.json()


async def test_devops_vps_latest_route(devops_client, auth_headers):
    resp = await devops_client.get("/api/v1/devops/vps/latest", headers=auth_headers)
    assert resp.status_code == 200
    assert "vps" in resp.json()


# ── LIVESYS Faz 5 Slice-1: autonomy-gate + ledger ──────────────


def _crit_alert(source="memory"):
    from app.core.devops_agent import Alert

    return Alert(
        id=f"{source}-1",
        severity="critical",
        source=source,
        message="test critical",
        value=99.0,
        threshold=85.0,
        timestamp="2026-06-04T00:00:00Z",
    )


async def _noop_webhook(*a, **k):
    return None


async def test_remediation_default_mode_is_notify():
    """GÜVENLİ DEFAULT: config.remediation_mode == 'notify' (otonom exec kapalı)."""
    from app.core.config import Settings

    assert Settings().remediation_mode == "notify"


async def test_remediate_notify_mode_does_not_execute(client, app):
    """notify mode: playbook YÜRÜTÜLMEZ; ledger'a executed=0 yazılır."""
    from app.core.devops_agent import DevOpsAgent

    db = app.state.db
    agent = DevOpsAgent(db=db, interval=60)
    agent._remediation_mode = "notify"
    agent._send_webhook = _noop_webhook
    calls = []

    async def fake_exec(cmd, timeout=30):
        calls.append(cmd)
        return {"stdout": "x", "exit_code": 0}

    agent._executor.execute = fake_exec
    await agent._remediate(_crit_alert("memory"))

    assert calls == []  # HİÇBİR komut çalışmadı
    rows = await db.fetch_all("SELECT executed, mode, success FROM remediation_log WHERE alert_source='memory'")
    assert len(rows) >= 1
    assert all(r["executed"] == 0 and r["mode"] == "notify" for r in rows)


async def test_remediate_auto_mode_executes(client, app):
    """auto mode (opt-in): playbook YÜRÜTÜLÜR; ledger'a executed=1 yazılır."""
    from app.core.devops_agent import DevOpsAgent

    db = app.state.db
    agent = DevOpsAgent(db=db, interval=60)
    agent._remediation_mode = "auto"
    agent._send_webhook = _noop_webhook
    calls = []

    async def fake_exec(cmd, timeout=30):
        calls.append(cmd)
        return {"stdout": "ok", "exit_code": 0}

    agent._executor.execute = fake_exec
    await agent._remediate(_crit_alert("disk"))

    assert len(calls) >= 1  # komut(lar) çalıştı
    rows = await db.fetch_all("SELECT executed, mode FROM remediation_log WHERE alert_source='disk'")
    assert len(rows) >= 1
    assert all(r["executed"] == 1 and r["mode"] == "auto" for r in rows)
