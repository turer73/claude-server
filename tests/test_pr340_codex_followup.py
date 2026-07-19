"""PR#342 Codex-P2 follow-up testleri — PR#340'ın iki ince kusuru.

Fix-1 (remediation.py): get_remediation_log merge ARTIK persisted-BAYRAĞI ile (timestamp
DEĞİL). Başarıyla-persist edilen kayıt mikrosaniye-ISO vs whole-second datetime('now')
farkıyla aynı-saniyede ÇİFT görünüyordu; repro base'de FAIL eder.

Fix-2 (metrics.py): _resolve_alert_db_by_source ZAMAN-SINIRLI — gecikmiş healthy-task,
SONRAKİ bir outage'ın yeni resolved=0 satırını yanlışlıkla kapatamaz.
"""

from unittest.mock import AsyncMock

from app.core.devops.models import RemediationRecord
from app.core.devops_agent import Alert, DevOpsAgent


class _RowsDB:
    def __init__(self, rows):
        self._rows = rows

    async def fetch_all(self, *a, **k):
        return self._rows

    async def execute(self, *a, **k):
        return None


def _mem_record(agent, ts, *, persisted, source="docker:n8n"):
    agent._remediation_log.append(
        RemediationRecord(
            timestamp=ts,
            alert_source=source,
            action="restart",
            command="c",
            result="r",
            success=True,
            persisted=persisted,
        )
    )


async def test_persisted_record_not_merged_dedup():
    # Fix-1: persisted=True (DB'ye yazıldı) → DB'de zaten var, merge etme (aynı-saniye çift-fix).
    db_rows = [{"id": 1, "alert_source": "docker:n8n", "action": "restart", "timestamp": "2026-07-19 10:00:00"}]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(agent, "2026-07-19T10:00:00.123456+00:00", persisted=True)  # aynı-saniye, mikrosaniye-fark

    got, source = await agent.get_remediation_log(limit=10)
    assert source == "db"  # ÇİFT yok — eski timestamp-merge "db+memory" + 2-satır dönerdi (repro)
    assert len(got) == 1


async def test_unpersisted_record_merged():
    # Fix-1 diğer yön: persisted=False (DB-yazımı transient-lock'la düştü) → merge (kayıp-kuyruk görünür).
    db_rows = [{"id": 1, "alert_source": "service:x", "action": "restart", "timestamp": "2026-07-19 10:00:00"}]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(agent, "2026-07-19T10:00:05+00:00", persisted=False)

    got, source = await agent.get_remediation_log(limit=10)
    assert source == "db+memory"
    assert got[0]["alert_source"] == "docker:n8n"  # kayıp in-memory kayıt önde
    assert got[1]["alert_source"] == "service:x"


async def test_merged_log_sorts_db_and_memory_before_limit():
    # PR#343 P2: önce concat+limit, yeni DB satırını eski memory satırının arkasında gizliyordu.
    db_rows = [
        {
            "id": 2,
            "alert_source": "service:newer-db",
            "action": "restart",
            "timestamp": "2026-07-19T10:00:05.900000+00:00",
        }
    ]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(
        agent,
        "2026-07-19T10:00:01.100000+00:00",
        persisted=False,
        source="service:older-memory",
    )

    got, source = await agent.get_remediation_log(limit=1)

    assert source == "db+memory"
    assert [row["alert_source"] for row in got] == ["service:newer-db"]


async def test_merged_log_keeps_newer_memory_before_limit():
    db_rows = [
        {
            "id": 1,
            "alert_source": "service:older-db",
            "action": "restart",
            "timestamp": "2026-07-19 10:00:01",
        }
    ]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(
        agent,
        "2026-07-19T10:00:05.100000+00:00",
        persisted=False,
        source="service:newer-memory",
    )

    got, source = await agent.get_remediation_log(limit=1)

    assert source == "db+memory"
    assert [row["alert_source"] for row in got] == ["service:newer-memory"]


async def test_db_log_sorts_event_time_before_limit(tmp_path, monkeypatch):
    # occurred_at ile insert/id sırası ayrışabilir. LIMIT olay-zamanı sırasından sonra uygulanmalı;
    # ayrıca legacy boşluklu SQLite ve yeni T-ayraçlı ISO biçimleri birlikte doğru sıralanmalı.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "event-order.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)
        await agent._persist_remediation_row(
            "service:newer-event",
            "critical",
            "notify",
            "newer",
            "noop",
            False,
            "skipped",
            None,
            occurred_at="2026-07-19 10:00:05.900000",
        )
        await agent._persist_remediation_row(
            "service:later-insert-older-event",
            "critical",
            "notify",
            "older",
            "noop",
            False,
            "skipped",
            None,
            occurred_at="2026-07-19T10:00:01.100000+00:00",
        )

        got, source = await agent.get_remediation_log(limit=1)

        assert source == "db"
        assert [row["alert_source"] for row in got] == ["service:newer-event"]
    finally:
        await db.close()
        get_settings.cache_clear()


async def test_apply_remediation_sets_persisted_and_no_duplicate(tmp_path, monkeypatch):
    # UÇTAN-UCA (gerçek DB): _apply_remediation başarıyla persist eder → record.persisted=True →
    # get_remediation_log dedup eder (source=db, tek-satır). Aynı-saniye çift-görünüm regresyonu.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "rem.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)
        alert = Alert(
            id="docker:n8n-1",
            severity="critical",
            source="docker:n8n",
            message="down",
            value=0,
            threshold=1,
            timestamp="t",
        )

        await agent._apply_remediation(alert, "docker:n8n", "Restart n8n", "docker restart n8n")
        record = agent._remediation_log[-1]
        assert record.persisted is True  # gerçek-DB yazımı başarılı → işaretlendi

        got, source = await agent.get_remediation_log(limit=10)
        assert source == "db"  # in-memory kayıt DB'de var → merge-dışı, ÇİFT yok
        assert len(got) == 1
        assert got[0]["timestamp"] == record.timestamp
    finally:
        await db.close()
        get_settings.cache_clear()


async def test_refused_remediation_sets_persisted_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "refused.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)
        agent._send_webhook = AsyncMock()
        alert = Alert(
            id="service:bad-1",
            severity="critical",
            source="service:bad;unit",
            message="down",
            value=0,
            threshold=1,
            timestamp="2026-07-19T10:00:00+00:00",
        )

        await agent._remediate_service("bad;unit", alert)

        record = agent._remediation_log[-1]
        assert record.persisted is True
        got, source = await agent.get_remediation_log(limit=10)
        assert source == "db"
        assert len(got) == 1
        assert got[0]["verify_status"] == "refused"
        assert got[0]["timestamp"] == record.timestamp
    finally:
        await db.close()
        get_settings.cache_clear()


async def test_resolve_bound_does_not_close_newer_outage(tmp_path, monkeypatch):
    # Fix-2 (gerçek DB): gecikmiş healthy-task (eski sample-time), SONRAKİ outage'ın yeni açık
    # satırını KAPATMAMALI. Eski timestamp-sınırsız UPDATE her açık satırı kapatıp gizlerdi.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "resolve.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)

        old_alert_t = "2026-07-19T10:00:00+00:00"
        healthy_t = "2026-07-19T10:00:30+00:00"  # eski outage ile yeni outage ARASINDA
        new_alert_t = "2026-07-19T10:05:00+00:00"  # sonraki outage (healthy-sample'dan SONRA)
        for ts in (old_alert_t, new_alert_t):
            await db.execute(
                "INSERT INTO alerts (timestamp, severity, source, message, resolved, valid_at) VALUES (?, ?, ?, ?, 0, ?)",
                (ts, "critical", "vps:offline", "VPS unreachable", ts),
            )

        # Gecikmiş healthy-task, sample-zamanı healthy_t ile koşuyor (yeni outage'dan HABERSİZ).
        await agent._resolve_alert_db_by_source("vps:offline", healthy_t)

        rows = await db.fetch_all("SELECT timestamp, resolved FROM alerts WHERE source='vps:offline' ORDER BY timestamp")
        by_ts = {r["timestamp"]: r["resolved"] for r in rows}
        assert by_ts[old_alert_t] == 1  # healthy-sample'dan önceki outage kapandı
        assert by_ts[new_alert_t] == 0  # SONRAKİ outage GİZLENMEDİ
    finally:
        # Repro-gate bu cleanup olmadan assertion-sonrası aiosqlite thread'inde asılı kalıyordu.
        await db.close()
        get_settings.cache_clear()


async def test_resolve_bound_closes_alert_at_or_before_healthy(tmp_path, monkeypatch):
    # Sınır dürüstlüğü: healthy-sample-anına EŞİT veya önceki alert kapanır (<=), sonraki değil.
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "resolve2.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)

        t = "2026-07-19T12:00:00+00:00"
        await db.execute(
            "INSERT INTO alerts (timestamp, severity, source, message, resolved, valid_at) VALUES (?, ?, ?, ?, 0, ?)",
            (t, "warning", "vps:qdrant", "container down", t),
        )
        await agent._resolve_alert_db_by_source("vps:qdrant", t)
        rows = await db.fetch_all("SELECT resolved FROM alerts WHERE source='vps:qdrant'")
        assert rows[0]["resolved"] == 1
    finally:
        await db.close()
        get_settings.cache_clear()


async def test_resolve_bound_preserves_fractional_and_microsecond_order(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_yaml_config", lambda path: {})
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.db.database import Database

    db = Database(str(tmp_path / "resolve-fractional.db"))
    try:
        await db.initialize()
        agent = DevOpsAgent(db=db, interval=60)
        source = "vps:fractional"
        healthy_t = "2026-07-19T10:00:00.100150+00:00"
        expected = {
            "2026-07-19T10:00:00.050000+00:00": 1,
            "2026-07-19T10:00:00.100100+00:00": 1,
            "2026-07-19T10:00:00.100200+00:00": 0,
            "2026-07-19T10:00:00.900000+00:00": 0,
        }
        for ts in expected:
            await db.execute(
                "INSERT INTO alerts (timestamp, severity, source, message, resolved, valid_at) VALUES (?, ?, ?, ?, 0, ?)",
                (ts, "critical", source, "fractional outage", ts),
            )

        await agent._resolve_alert_db_by_source(source, healthy_t)

        rows = await db.fetch_all(
            "SELECT timestamp, resolved FROM alerts WHERE source = ? ORDER BY timestamp",
            (source,),
        )
        assert {row["timestamp"]: row["resolved"] for row in rows} == expected
    finally:
        await db.close()
        get_settings.cache_clear()
