"""disc#1353abc testleri — probe-alert ilk-an kalıcılığı + sessiz-yutma logları + DB-ledger endpoint.

Repro-test (base'de FAIL): test_service_down_alert_persisted_first_moment — fix öncesi probe.py
service/docker/vps alert'lerini _store_alert'e HİÇ geçirmiyordu (yalnız metrik-yolu yazıyordu);
ilk "X down" anı ne alerts-tablosunda ne events/Telegram-hattında görünüyordu.
"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

from app.core.devops.models import Alert
from app.core.devops_agent import DevOpsAgent


async def test_service_down_alert_persisted_first_moment():
    # disc#1353a: service-down tespiti ANINDA _store_alert'e gitmeli (alerts-DB + events-köprüsü).
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_containers = []  # yalnız service-yolu

    async def mock_exec(cmd, timeout=5):
        if "systemctl is-active" in cmd:
            return {"stdout": "inactive\n", "stderr": "", "exit_code": 3}
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_remediate_service", new_callable=AsyncMock),
    ):
        await agent._check_services()
        await asyncio.sleep(0)  # create_task'leri akıt

    assert store.call_count >= 1, "service-down alert'i ilk-anda _store_alert'e gitmedi (disc#1353a)"
    stored_sources = {c.args[0].source for c in store.call_args_list}
    assert any(s.startswith("service:") for s in stored_sources)


async def test_container_down_alert_persisted_first_moment():
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_services = []
    agent._critical_containers = ["qdrant"]

    async def mock_exec(cmd, timeout=5):
        if "docker ps" in cmd:
            return {"stdout": "", "stderr": "", "exit_code": 0}  # container yok = down
        return {"stdout": "", "stderr": "", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_remediate_container", new_callable=AsyncMock),
    ):
        await agent._check_services()
        await asyncio.sleep(0)

    stored_sources = {c.args[0].source for c in store.call_args_list}
    assert "docker:qdrant" in stored_sources


def _mk_alert(source: str, severity: str = "critical") -> Alert:
    return Alert(id=f"{source}-1", severity=severity, source=source, message="x", value=0, threshold=1, timestamp="t")


async def test_vps_recovery_resolves_alerts_in_db():
    # disc#1353a-devam: yaşam-döngüsü tam olmalı — başarılı probe, önceki vps:offline/wan-down
    # ve container-alert'lerini DB'de de resolved işaretlemeli (yalnız in-memory pop değil).
    agent = DevOpsAgent(db=None, interval=60)
    agent._vps_containers = ["qdrant", "n8n"]
    agent._active_alerts["vps:offline"] = _mk_alert("vps:offline")
    agent._active_alerts["vps:qdrant"] = _mk_alert("vps:qdrant", "warning")

    sample = {"cpu": 1.0, "mem": 2.0, "disk": 3.0, "containers_total": 2, "containers_up": 2, "names": ["qdrant", "n8n"]}
    with (
        patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=sample),
        patch.object(agent, "_store_vps_metrics", new_callable=AsyncMock),
        patch.object(agent, "_store_alert", new_callable=AsyncMock),
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock) as resolve,
    ):
        await agent._check_vps()
        await asyncio.sleep(0)

    resolved_sources = {c.args[0] for c in resolve.call_args_list}
    assert "vps:offline" in resolved_sources
    assert "vps:qdrant" in resolved_sources
    for c in resolve.call_args_list:
        assert c.args[1]  # resolved_at dolu
    assert "vps:offline" not in agent._active_alerts
    assert "vps:qdrant" not in agent._active_alerts


async def test_vps_recovery_resolves_db_even_after_restart():
    # Codex-P2 (PR#340 follow-up): alert DB'ye yazıldıktan sonra healthy-probe'dan ÖNCE restart
    # olursa _active_alerts BOŞ başlar — resolve yine de DB'ye gitmeli (kaynak-bazlı, in-memory
    # Alert nesnesine bağlı DEĞİL). Aksi halde DB satırı sonsuza dek resolved=0 (stuck-open).
    agent = DevOpsAgent(db=None, interval=60)
    agent._vps_containers = ["qdrant"]
    assert not agent._active_alerts  # restart-sonrası temiz bellek

    sample = {"cpu": 1.0, "mem": 2.0, "disk": 3.0, "containers_total": 1, "containers_up": 1, "names": ["qdrant"]}
    with (
        patch.object(agent, "_vps_ssh_probe", new_callable=AsyncMock, return_value=sample),
        patch.object(agent, "_store_vps_metrics", new_callable=AsyncMock),
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock) as resolve,
    ):
        await agent._check_vps()
        await asyncio.sleep(0)

    resolved_sources = {c.args[0] for c in resolve.call_args_list}
    assert {"vps:offline", "klipper:wan-down", "vps:qdrant"} <= resolved_sources  # koşulsuz idempotent-resolve


class _FailingDB:
    async def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    async def fetch_all(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


async def test_store_alert_db_failure_logged_not_silent(caplog):
    # disc#1353b: 07-18'de 6-saatlik DB-lock penceresi kayıtları SESSİZCE yuttu — artık log'lu,
    # ve log yalnız tip-adı taşır (PR#339 Codex-dersi: exception-str payload sızdırabilir).
    agent = DevOpsAgent(db=_FailingDB(), interval=60)
    with (
        patch("app.core.devops.metrics.emit_event"),
        caplog.at_level("WARNING", logger="devops_agent"),
    ):
        await agent._store_alert(_mk_alert("service:n8n"))
    assert any("alerts insert failed" in r.message for r in caplog.records)
    assert not any("database is locked" in r.getMessage() for r in caplog.records)  # tip-adı-only


async def test_remediation_persist_failure_logged(caplog):
    agent = DevOpsAgent(db=_FailingDB(), interval=60)
    with caplog.at_level("WARNING", logger="devops_agent"):
        await agent._persist_remediation_row("docker:n8n", "critical", "auto", "restart", "docker restart n8n", True, "x", True)
    assert any("remediation_log insert failed" in r.message for r in caplog.records)


class _RowsDB:
    def __init__(self, rows):
        self._rows = rows

    async def fetch_all(self, *a, **k):
        return self._rows

    async def execute(self, *a, **k):
        return None


async def test_remediation_log_reads_persistent_db():
    # disc#1353c: endpoint-yolu kalıcı remediation_log tablosundan okumalı — in-memory deque
    # her restart'ta sıfırlanıyordu, dashboard 83-satırlık gerçek geçmişi hiç göremiyordu.
    rows = [{"id": 83, "alert_source": "docker:n8n", "action": "restart", "success": 1, "timestamp": "2026-07-17 07:38:00"}]
    agent = DevOpsAgent(db=_RowsDB(rows), interval=60)
    got, source = await agent.get_remediation_log(limit=10)
    assert source == "db"
    assert got[0]["alert_source"] == "docker:n8n"


async def test_remediation_log_falls_back_to_memory_honestly(caplog):
    agent = DevOpsAgent(db=_FailingDB(), interval=60)
    with caplog.at_level("WARNING", logger="devops_agent"):
        got, source = await agent.get_remediation_log(limit=10)
    assert source == "memory"  # kaynak DÜRÜSTÇE etiketli — sessiz-maskeleme yok
    assert any("remediation_log read failed" in r.message for r in caplog.records)
