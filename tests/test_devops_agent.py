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


async def test_metrics_history_window_isot_format(devops_client, app):
    """Regresyon: metrics_history.timestamp Python isoformat() ile ISO-T ('T'-ayraçlı,
    +00:00) yazılır; AMA schema DEFAULT'u datetime('now') = BOŞLUK-ayraçlı. Ham string-
    compare iki formatı karıştırır ('T'(0x54) vs ' '(0x20)) → yanlış pencere. Fix
    datetime(timestamp) ile her iki formatı UTC'ye normalize eder.

    Test üç vakayı birden ayırt eder:
      - taze ISO-T satır → pencerede (her zaman doğru olmalı)
      - aynı-gün/eski ISO-T satır → DIŞARDA (ham-compare bunu yanlışça ALIRDI)
      - taze BOŞLUK-format satır (schema-default taklidi) → İÇERDE (Codex P2: replace()-fix
        bunu yanlışça DIŞLARDI; format-agnostik datetime() yakalar)
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    # UTC gece-yarısı kenarı: 30dk-eski satır aynı UTC-günde kurulamaz → ayrımı atla
    if now.hour == 0 and now.minute < 35:
        pytest.skip("UTC gece-yarısı penceresi — aynı-gün eski-satır kurulamıyor")

    db = app.state.db
    recent = (now - timedelta(minutes=5)).isoformat()  # ISO-T, pencerede
    # Aynı UTC-günün başı: kesinlikle >30dk eski ama datetime('now') ile AYNI tarih-öneki
    old_sameday = now.replace(hour=0, minute=0, second=1, microsecond=0).isoformat()
    # BOŞLUK-format taze satır (schema DEFAULT datetime('now') taklidi): 'T' yok, tz yok
    recent_space = (now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")

    for ts in (recent, old_sameday, recent_space):
        await db.execute(
            "INSERT INTO metrics_history "
            "(timestamp, cpu_usage, memory_usage, disk_usage, temperature, load_avg, network_io) "
            "VALUES (?, 0, 0, 0, 0, '[]', '{}')",
            (ts,),
        )

    agent = app.state.devops_agent
    got = {r["timestamp"] for r in await agent.get_metrics_history(minutes=30)}
    assert recent in got  # taze ISO-T satır pencerede
    assert old_sameday not in got  # BUG olsaydı ham-compare bunu yanlışça alırdı
    assert recent_space in got  # Codex P2: boşluk-format taze satır da yakalanmalı


async def test_metrics_window_uses_expression_index(devops_client, app):
    """Codex P2: format-agnostik datetime(timestamp) predikatı RANGE-SEARCH yapabilmeli
    (full-SCAN değil). Expression index idx_metrics_dt schema'da olmalı ve sorgu planı
    onu kullanmalı — aksi halde pencere<500 satırda tüm tarih taranır."""
    db = app.state.db
    # 1) expression index'ler schema'da mevcut
    idx = {
        r["name"]
        for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_metrics_dt','idx_vps_metrics_dt')")
    }
    assert idx == {"idx_metrics_dt", "idx_vps_metrics_dt"}, f"expression index eksik: {idx}"

    # 2) sorgu planı expression index'i kullanıyor (SCAN değil SEARCH)
    plan = " ".join(
        r["detail"]
        for r in await db.fetch_all(
            "EXPLAIN QUERY PLAN SELECT * FROM metrics_history "
            "WHERE datetime(timestamp) > datetime('now', '-30 minutes') "
            "ORDER BY datetime(timestamp) DESC LIMIT 500"
        )
    )
    assert "idx_metrics_dt" in plan, f"expression index kullanılmıyor: {plan}"


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


async def test_detect_cpu_sustained_critical():
    # #567 sustained-gating: SÜRDÜRÜLEN yüksek CPU → critical. _history'de N örnek
    # eşik-üstü olmalı (prod'da _tick append eder; testte elle doldur).
    from app.core.devops_agent import _SUSTAINED_N, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    hi = {"cpu_percent": 95, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    agent._history.extend([dict(hi) for _ in range(_SUSTAINED_N)])
    alerts = agent._detect(hi)
    cpu = [a for a in alerts if a.source == "cpu"]
    assert len(cpu) == 1
    assert cpu[0].severity == "critical"


async def test_detect_cpu_transient_warning():
    # #567 FP: GEÇİCİ zirve (zamanlanmış ağır iş — test-runner/e2e) tek-örnekte critical
    # ÜRETMEMELİ. Yeterli sürdürülen-geçmiş yok → warning (remediate/escalate yok).
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 98, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    alerts = agent._detect(metrics)
    cpu = [a for a in alerts if a.source == "cpu"]
    assert len(cpu) == 1
    assert cpu[0].severity == "warning"  # eşik-üstü ama sürdürülmemiş → critical DEĞİL


async def test_detect_cpu_warning_upgrades_to_critical():
    # Codex P1: ilk geçici-warning aktif-slotu tutar; SÜRDÜRÜLEN olunca critical'e
    # YÜKSELMELİ (yoksa gerçek sürekli-yük warning'de takılı kalır, escalate olmaz).
    from app.core.devops_agent import _SUSTAINED_N, DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 98, "memory_percent": 40, "disk_percent": 50, "temperature": 45}
    a1 = agent._detect(metrics)  # history boş → transient warning, aktif-slot dolu
    assert [a for a in a1 if a.source == "cpu"][0].severity == "warning"
    agent._history.extend([dict(metrics) for _ in range(_SUSTAINED_N)])  # artık sürdürülen
    a2 = agent._detect(metrics)
    cpu = [a for a in a2 if a.source == "cpu"]
    assert len(cpu) == 1
    assert cpu[0].severity == "critical"  # warning→critical yükseltildi + emit


async def test_detect_temperature_single_critical_not_gated():
    # temperature sustained-gating'e TABİ DEĞİL (fiziksel — tek yüksek okuma gerçek).
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    metrics = {"cpu_percent": 20, "memory_percent": 40, "disk_percent": 50, "temperature": 95}
    alerts = agent._detect(metrics)
    temp = [a for a in alerts if a.source == "temperature"]
    assert len(temp) == 1
    assert temp[0].severity == "critical"  # tek-örnek bile critical (gated değil)


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


async def test_playbooks_no_destructive_steps():
    """FAZ5 safening regresyon-guard: hiçbir playbook adımı yıkıcı/geri-alınamaz
    olmamalı — volume veri-silme (--volumes), otonom backup-silme, rm -rf yasak.
    (auto-mode'da false-positive critical'de veri kaybını önler.)"""
    from app.core.devops_agent import PLAYBOOKS

    for key, steps in PLAYBOOKS.items():
        for step in steps:
            cmd = step["cmd"]
            assert "--volumes" not in cmd, f"{key}: --volumes (volume veri-silme) yasak"
            assert "backup" not in cmd.lower(), f"{key}: otonom backup-silme yasak"
            assert "rm -rf" not in cmd, f"{key}: rm -rf yasak"


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


async def test_check_containers_unhealthy_alerts():
    """Codex P2: 'Up (unhealthy)' -> 'Up' içerse de kritik alert (çalışıyor-ama-bozuk)."""
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_containers = ["qdrant"]
    agent._critical_services = []  # sadece container'a odaklan

    async def mock_exec(cmd, timeout=5):
        if "docker ps" in cmd:
            return {"stdout": "Up 2 hours (unhealthy)\n", "stderr": "", "exit_code": 0}
        if "docker start" in cmd:
            return {"stdout": "", "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
        await agent._check_services()

    assert "docker:qdrant" in agent._active_alerts
    assert "UNHEALTHY" in agent._active_alerts["docker:qdrant"].message


async def test_verify_remediation_docker_health_aware():
    """Codex P2: restart sonrası Running=true ama unhealthy -> verify FALSE (false-recovery yok).
    healthy/none -> True; unhealthy/stopped -> False."""
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)

    async def _inspect(out):
        async def mock_exec(cmd, timeout=10):
            return {"stdout": out, "stderr": "", "exit_code": 0}

        with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
            return await agent._verify_remediation("docker:qdrant")

    assert await _inspect("true;healthy\n") is True
    assert await _inspect("true;none\n") is True  # healthcheck'siz -> Running yeter
    assert await _inspect("true;unhealthy\n") is False  # çalışıyor ama bozuk
    assert await _inspect("false;none\n") is False  # durmuş
    assert await _inspect("true;starting\n") is None  # Codex P2: start_period -> belirsiz, escalate yok


async def test_check_containers_up_healthy_no_alert():
    """'Up (healthy)' -> alarm YOK (unhealthy substring'i healthy'de yok)."""
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_containers = ["qdrant"]
    agent._critical_services = []

    async def mock_exec(cmd, timeout=5):
        if "docker ps" in cmd:
            return {"stdout": "Up 2 hours (healthy)\n", "stderr": "", "exit_code": 0}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
        await agent._check_services()

    assert "docker:qdrant" not in agent._active_alerts


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
    agent._vps_fail_threshold = 1  # alert-mantığını izole test et (sustained-gate ayrı testte)

    # Probe failed but klipper's own internet is up → genuine VPS outage.
    with (
        patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=None),
        patch.object(agent, "_local_internet_up", new_callable=AsyncMock, return_value=True),
    ):
        await agent._check_vps()

    rows = await db.fetch_all("SELECT * FROM vps_metrics_history")
    assert len(rows) == 1
    assert rows[0]["online"] == 0
    assert "vps:offline" in agent._active_alerts
    assert "klipper:wan-down" not in agent._active_alerts
    assert agent.latest_vps["online"] is False
    await db.close()
    get_settings.cache_clear()


async def test_check_vps_local_wan_down(tmp_path, monkeypatch):
    """Probe fails because klipper itself lost internet → blame WAN, not the VPS."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps_wan.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    agent._vps_fail_threshold = 1  # alert-mantığını izole test et (sustained-gate ayrı testte)

    with (
        patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=None),
        patch.object(agent, "_local_internet_up", new_callable=AsyncMock, return_value=False),
    ):
        await agent._check_vps()

    # No false VPS-offline alert; a distinct klipper-WAN alert instead.
    assert "klipper:wan-down" in agent._active_alerts
    assert "vps:offline" not in agent._active_alerts
    assert agent.latest_vps["online"] is False
    await db.close()
    get_settings.cache_clear()


async def test_check_vps_sustained_gate(tmp_path, monkeypatch):
    """Tek geçici probe-fail vps:offline ÜRETMEZ — N-ardışık-fail gerek (2026-06-19 fix).
    Başarılı probe sayacı sıfırlar → tek-blip'ler birikmez."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps_gate.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    agent._vps_fail_threshold = 2  # default

    with (
        patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=None),
        patch.object(agent, "_local_internet_up", new_callable=AsyncMock, return_value=True),
    ):
        await agent._check_vps()  # 1. fail → gate altında, alert YOK
        assert "vps:offline" not in agent._active_alerts
        assert agent._vps_probe_fails == 1
        await agent._check_vps()  # 2. ardışık fail → eşik → alert
        assert "vps:offline" in agent._active_alerts

    # Başarılı probe → sayaç sıfır + alert temizlenir
    probe = {"names": [], "cpu_percent": 5, "memory_percent": 10, "disk_percent": 20}
    with patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=probe):
        await agent._check_vps()
    assert agent._vps_probe_fails == 0
    assert "vps:offline" not in agent._active_alerts
    await db.close()
    get_settings.cache_clear()


async def test_check_vps_recovery_clears_wan_alert(tmp_path, monkeypatch):
    """A successful probe clears a prior klipper:wan-down alert."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.devops_agent import Alert, DevOpsAgent
    from app.db.database import Database

    db = Database(str(tmp_path / "vps_recover.db"))
    await db.initialize()
    agent = DevOpsAgent(db=db, interval=60)
    agent._active_alerts["klipper:wan-down"] = Alert(
        id="klipper:wan-down-0",
        severity="critical",
        source="klipper:wan-down",
        message="stale",
        value=0,
        threshold=1,
        timestamp="2026-06-17T00:00:00+00:00",
    )

    probe = {"cpu": 10.0, "mem": 20.0, "disk": 30.0, "containers_total": 5, "containers_up": 5, "names": []}
    with patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=probe):
        await agent._check_vps()

    assert "klipper:wan-down" not in agent._active_alerts
    await db.close()
    get_settings.cache_clear()


async def test_vps_ssh_probe_no_host():
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    agent._vps_host = ""
    assert await agent._vps_ssh_probe() is None


async def test_local_internet_up_reachable():
    """First TCP connect succeeds → internet up, no second target tried."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch("asyncio.open_connection", new_callable=AsyncMock, return_value=(MagicMock(), writer)) as oc:
        assert await agent._local_internet_up() is True
    writer.close.assert_called_once()
    assert oc.await_count == 1  # short-circuits after the first success


async def test_local_internet_up_down():
    """Every target fails (timeout/refused) → internet down."""
    from unittest.mock import AsyncMock, patch

    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    with patch("asyncio.open_connection", new_callable=AsyncMock, side_effect=OSError("refused")) as oc:
        assert await agent._local_internet_up() is False
    assert oc.await_count == 2  # both anycast targets attempted


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


async def test_remediate_service_notify_mode_does_not_execute(client, app):
    """Codex P1: servis/container remediation da gate'li — notify'da systemctl YOK."""
    from app.core.devops_agent import DevOpsAgent

    db = app.state.db
    agent = DevOpsAgent(db=db, interval=60)
    agent._remediation_mode = "notify"
    agent._send_webhook = _noop_webhook
    calls = []

    async def fake_exec(cmd, timeout=30):
        calls.append(cmd)
        return {"stdout": "", "exit_code": 0}

    agent._executor.execute = fake_exec
    await agent._remediate_service("linux-ai-server", _crit_alert("service:linux-ai-server"))

    assert calls == []  # systemctl restart YÜRÜTÜLMEDİ
    rows = await db.fetch_all("SELECT executed, mode FROM remediation_log WHERE alert_source='service:linux-ai-server'")
    assert len(rows) >= 1
    assert all(r["executed"] == 0 for r in rows)


# ── LIVESYS Faz 5 Slice-2: verify -> escalate ──────────────


def _auto_agent(db):
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=db, interval=60)
    agent._remediation_mode = "auto"
    agent._verify_grace = 0  # test: grace-sleep yok
    agent._send_webhook = _noop_webhook

    async def fake_exec(cmd, timeout=30):
        return {"stdout": "ok", "exit_code": 0}

    agent._executor.execute = fake_exec
    return agent


async def test_verify_pass_metric_below_threshold(client, app):
    """auto + verify metrik eşik-altı -> verify_status=pass, escalated=0."""
    db = app.state.db
    agent = _auto_agent(db)
    agent._monitor.collect_metrics = lambda: {"memory_percent": 10.0}  # eşik(85) altı
    await agent._remediate(_crit_alert("memory"))

    rows = await db.fetch_all("SELECT verify_status, escalated FROM remediation_log WHERE alert_source='memory'")
    assert rows
    assert all(r["verify_status"] == "pass" and r["escalated"] == 0 for r in rows)


async def test_verify_fail_escalates(client, app):
    """auto + verify metrik hâlâ eşik-üstü -> verify_status=fail, escalated=1 + escalate event."""
    from app.core.events import recent_events

    db = app.state.db
    agent = _auto_agent(db)
    agent._monitor.collect_metrics = lambda: {"memory_percent": 99.0}  # eşik(85) üstü -> fail
    await agent._remediate(_crit_alert("memory"))

    rows = await db.fetch_all("SELECT verify_status, escalated FROM remediation_log WHERE alert_source='memory'")
    assert rows
    assert all(r["verify_status"] == "fail" and r["escalated"] == 1 for r in rows)
    # escalate -> critical event yazıldı
    evs = recent_events(min_severity="critical")
    assert any("remediation:memory" in (e.get("source") or "") for e in evs)


async def test_verify_skipped_in_notify_mode(client, app):
    """notify mode: exec yok -> verify de yok; satırlar 'skipped', escalated=0."""
    from app.core.devops_agent import DevOpsAgent

    db = app.state.db
    agent = DevOpsAgent(db=db, interval=60)  # default notify
    agent._verify_grace = 0
    agent._send_webhook = _noop_webhook
    await agent._remediate(_crit_alert("disk"))

    rows = await db.fetch_all("SELECT verify_status, escalated, executed FROM remediation_log WHERE alert_source='disk'")
    assert rows
    assert all(r["verify_status"] == "skipped" and r["escalated"] == 0 and r["executed"] == 0 for r in rows)


async def test_escalate_persistent_critical_after_interval(monkeypatch):
    """Çözülmeyen critical alert interval sonrası re-escalate eder. emit_event mock'lanır
    (cross-connection db-race yok -> CI-deterministik)."""
    import time as _t

    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._active_alerts["memory"] = _crit_alert("memory")
    # monotonic-göreceli geçmiş (0.0 DEĞİL — taze-boot'ta monotonic küçük -> elapsed
    # yanlış hesaplanır, CI-fail; interval+10s öncesi her sistemde elapsed>=interval).
    agent._last_escalation["memory"] = _t.monotonic() - agent._escalation_interval - 10
    calls = []
    monkeypatch.setattr(da, "emit_event", lambda **kw: calls.append(kw))
    await agent._escalate_persistent()

    assert any(c.get("source") == "escalation:memory" and c.get("severity") == "critical" for c in calls)


async def test_escalate_persistent_first_seen_no_escalate(monkeypatch):
    """İlk-görülme escalate ETMEZ (saat başlar); interval içinde de tekrar etmez."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._active_alerts["cpu"] = _crit_alert("cpu")  # _last_escalation'da YOK
    calls = []
    monkeypatch.setattr(da, "emit_event", lambda **kw: calls.append(kw))
    await agent._escalate_persistent()
    assert calls == []  # ilk-görülme -> init, escalate yok
    assert "cpu" in agent._last_escalation  # saat başlatıldı
    await agent._escalate_persistent()  # interval içinde -> hâlâ yok
    assert calls == []


async def test_escalate_persistent_nonmetric_source(monkeypatch):
    """Codex P2: metrik-DIŞI kaynak (service:*, _detect-dışı) da escalate eder (uniform-init)."""
    import time as _t

    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._active_alerts["service:linux-ai-server"] = _crit_alert("service:linux-ai-server")
    # monotonic-göreceli geçmiş (taze-boot CI-safe, 0.0 değil)
    agent._last_escalation["service:linux-ai-server"] = _t.monotonic() - agent._escalation_interval - 10
    calls = []
    monkeypatch.setattr(da, "emit_event", lambda **kw: calls.append(kw))
    await agent._escalate_persistent()

    assert any(c.get("source") == "escalation:service:linux-ai-server" for c in calls)


async def test_escalate_skips_acked_source(client, app, monkeypatch):
    """ACK'lenmiş kaynak re-escalate ETMEZ (nag-etme). events.acked=1 -> skip."""
    import time as _t

    from app.core import devops_agent as da

    db = app.state.db
    agent = da.DevOpsAgent(db=db, interval=60)
    agent._active_alerts["memory"] = _crit_alert("memory")
    agent._last_escalation["memory"] = _t.monotonic() - agent._escalation_interval - 10
    # acked event (aynı async-conn ile insert -> cross-conn race yok)
    await db.execute("INSERT INTO events (type, source, severity, title, acked) VALUES ('alert','memory','critical','x',1)")
    calls = []
    monkeypatch.setattr(da, "emit_event", lambda **kw: calls.append(kw))
    await agent._escalate_persistent()
    assert calls == []  # acked -> escalate YOK


async def test_source_acked_no_db_returns_false():
    """db yoksa _source_acked False (fail-loud: escalate-devam)."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    assert await agent._source_acked("memory") is False


async def test_source_acked_db_error_returns_false():
    """db.fetch_one hata atarsa _source_acked False (escalate susmaz)."""
    from app.core import devops_agent as da

    class _BadDB:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("boom")

    agent = da.DevOpsAgent(db=_BadDB(), interval=60)
    assert await agent._source_acked("memory") is False


# ── Slice-2: force_remediate ([🔧 Uygula]) ──────────────────────


class _FakeExec:
    """Komutları çalıştırmadan kaydeder (gerçek docker/systemctl YOK)."""

    def __init__(self, exit_code=0):
        self.cmds = []
        self.exit_code = exit_code

    async def execute(self, cmd, timeout=30):
        self.cmds.append(cmd)
        return {"exit_code": self.exit_code, "stdout": "fake-ok"}


def test_executable_playbook_resolution():
    """has_actionable_playbook: aksiyon-küme doğru (cpu/cron hariç)."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    assert agent.has_actionable_playbook("memory") is True
    assert agent.has_actionable_playbook("disk") is True
    assert agent.has_actionable_playbook("temperature") is True
    assert agent.has_actionable_playbook("service:linux-ai-server") is True
    assert agent.has_actionable_playbook("docker:n8n") is True
    assert agent.has_actionable_playbook("escalation:memory") is True  # iç-kaynağa iner
    assert agent.has_actionable_playbook("cpu") is False  # sadece-inceleme
    assert agent.has_actionable_playbook("cron:backup") is False


def test_executable_playbook_rejects_shell_injection_name():
    """GÜVENLİK: service/docker adında shell-metakarakter -> aksiyon YOK (RCE guard)."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    assert agent.has_actionable_playbook("service:x; rm -rf /") is False
    assert agent.has_actionable_playbook("docker:n8n && curl evil") is False
    assert agent.has_actionable_playbook("service:$(whoami)") is False
    # meşru systemd templated unit (@) ve normal container geçer
    assert agent.has_actionable_playbook("service:getty@tty1") is True
    assert agent.has_actionable_playbook("docker:uptime-kuma") is True


async def test_force_remediate_executes_and_verifies_pass(monkeypatch):
    """[🔧 Uygula] memory -> playbook çalışır + verify pass."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._executor = _FakeExec(exit_code=0)
    agent._verify_grace = 0

    async def _ok(source):
        return True

    monkeypatch.setattr(agent, "_verify_remediation", _ok)
    res = await agent.force_remediate("memory")
    assert res["ok"] is True
    assert res["executed"] is True
    assert res["all_success"] is True
    assert res["verify"] == "pass"
    assert any("prune" in c for c in agent._executor.cmds)


async def test_force_remediate_service_strips_escalation_prefix(monkeypatch):
    """escalation:service:X -> service:X restart komutu üretilir."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._executor = _FakeExec(exit_code=0)
    agent._verify_grace = 0

    async def _ok(source):
        return True

    monkeypatch.setattr(agent, "_verify_remediation", _ok)
    res = await agent.force_remediate("escalation:service:linux-ai-server")
    assert res["source"] == "service:linux-ai-server"
    assert any("systemctl restart linux-ai-server" in c for c in agent._executor.cmds)


async def test_force_remediate_no_actionable_playbook():
    """cpu -> çalıştırılacak aksiyon yok (executed False, exec YOK)."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._executor = _FakeExec()
    res = await agent.force_remediate("cpu")
    assert res["ok"] is True
    assert res["executed"] is False
    assert res["reason"] == "no_actionable_playbook"
    assert agent._executor.cmds == []  # hiç komut çalışmadı


async def test_force_remediate_verify_fail_escalates(monkeypatch):
    """verify fail -> remediation:source critical event emit (escalate)."""
    from app.core import devops_agent as da

    agent = da.DevOpsAgent(db=None, interval=60)
    agent._executor = _FakeExec(exit_code=0)
    agent._verify_grace = 0

    async def _fail(source):
        return False

    monkeypatch.setattr(agent, "_verify_remediation", _fail)
    emitted = []
    monkeypatch.setattr(da, "emit_event", lambda **kw: emitted.append(kw))
    res = await agent.force_remediate("memory")
    assert res["verify"] == "fail"
    assert any(e.get("source") == "remediation:memory" and e.get("severity") == "critical" for e in emitted)


async def test_force_remediate_endpoint_resolves_event_id(devops_client, app, auth_headers, monkeypatch):
    """POST /remediate/force {event_id} -> events.source çözer -> force_remediate çağırır.
    Gerçek shell YOK (executor fake)."""
    db = app.state.db
    await db.execute("INSERT INTO events (type, source, severity, title) VALUES ('alert','memory','critical','x')")
    row = await db.fetch_one("SELECT MAX(id) AS id FROM events WHERE source='memory'")
    eid = row["id"]
    agent = app.state.devops_agent
    agent._executor = _FakeExec(exit_code=0)
    agent._verify_grace = 0

    async def _ok(source):
        return True

    monkeypatch.setattr(agent, "_verify_remediation", _ok)
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={"event_id": eid}, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "memory"
    assert data["executed"] is True


async def test_force_remediate_endpoint_requires_auth(devops_client):
    """Auth'suz force-remediate -> 401/403 (RCE-yüzeyi korunur)."""
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={"source": "memory"})
    assert resp.status_code in (401, 403)


async def test_force_remediate_endpoint_event_not_found(devops_client, auth_headers):
    """Var-olmayan event_id -> 404."""
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={"event_id": 999999}, headers=auth_headers)
    assert resp.status_code == 404


async def test_force_remediate_endpoint_no_params(devops_client, auth_headers):
    """source/event_id yok -> 400."""
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={}, headers=auth_headers)
    assert resp.status_code == 400


async def test_force_remediate_endpoint_no_agent(devops_client, app, auth_headers):
    """Agent başlatılmamış -> 503."""
    app.state.devops_agent = None
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={"source": "memory"}, headers=auth_headers)
    assert resp.status_code == 503


async def test_force_remediate_endpoint_no_db(devops_client, app, auth_headers):
    """event_id verildi ama db yok -> 503."""
    app.state.db = None
    resp = await devops_client.post("/api/v1/devops/remediate/force", json={"event_id": 1}, headers=auth_headers)
    assert resp.status_code == 503


async def test_is_in_cpu_grace_window_env_toggle(monkeypatch):
    # klipper #100224: CPU-grace penceresi UTC-saat env'leriyle deterministik kontrol edilir.
    from app.core.devops_agent import DevOpsAgent

    agent = DevOpsAgent(db=None, interval=60)
    monkeypatch.setenv("CPU_GRACE_START_HOUR", "0")
    monkeypatch.setenv("CPU_GRACE_END_HOUR", "24")  # 0<=hour<24 → her saat
    assert agent._is_in_cpu_grace_window() is True
    monkeypatch.setenv("CPU_GRACE_END_HOUR", "0")  # 0<=hour<0 → asla
    assert agent._is_in_cpu_grace_window() is False
    monkeypatch.setenv("CPU_GRACE_START_HOUR", "abc")  # geçersiz → fail-safe False
    assert agent._is_in_cpu_grace_window() is False


async def test_detect_cpu_grace_suppresses_cpu_only(monkeypatch):
    # klipper #100224: grace-penceresinde SADECE cpu-alarmı bastırılır (meşru test-runner/e2e
    # yükü); disk yine izlenir. Pencere-dışı aynı metrik cpu-alarmı üretir (kontrol).
    from app.core.devops_agent import _SUSTAINED_N, DevOpsAgent

    hi = {"cpu_percent": 95, "memory_percent": 40, "disk_percent": 95, "temperature": 45}

    # Pencere KAPALI → cpu-alarmı VAR (kontrol)
    monkeypatch.setenv("CPU_GRACE_START_HOUR", "0")
    monkeypatch.setenv("CPU_GRACE_END_HOUR", "0")
    a_off = DevOpsAgent(db=None, interval=60)
    a_off._history.extend([dict(hi) for _ in range(_SUSTAINED_N)])
    assert any(a.source == "cpu" for a in a_off._detect(hi))

    # Pencere AÇIK → cpu-alarmı YOK, disk-alarmı yine VAR
    monkeypatch.setenv("CPU_GRACE_END_HOUR", "24")
    a_on = DevOpsAgent(db=None, interval=60)
    a_on._history.extend([dict(hi) for _ in range(_SUSTAINED_N)])
    alerts = a_on._detect(hi)
    assert [a for a in alerts if a.source == "cpu"] == []
    assert any(a.source == "disk" for a in alerts)
