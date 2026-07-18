"""PR#340 Codex-P2 follow-up testleri — DB+memory merge (transient-lock kaybı görünür kalsın).

Repro: get_remediation_log salt-DB okuyordu — _persist_remediation_row transient-lock'la
düşmüş bir kayıt yalnız in-memory'deyken endpoint onu GÖRÜNMEZ kılıyordu (eski-endpoint
gösteriyordu; regresyon). Şimdi en-yeni-DB-satırından sonraki in-memory kuyruk merge edilir.
"""

from app.core.devops.models import RemediationRecord
from app.core.devops_agent import DevOpsAgent


class _RowsDB:
    def __init__(self, rows):
        self._rows = rows

    async def fetch_all(self, *a, **k):
        return self._rows

    async def execute(self, *a, **k):
        return None


def _mem_record(agent, ts):
    agent._remediation_log.append(
        RemediationRecord(timestamp=ts, alert_source="docker:n8n", action="restart", command="c", result="r", success=True)
    )


async def test_remediation_log_merges_memory_tail_newer_than_db():
    # DB'nin en-yenisi 10:00; in-memory'de 11:00 kaydı var (DB-yazımı lock'la düşmüş senaryo)
    db_rows = [{"id": 1, "alert_source": "service:x", "action": "restart", "timestamp": "2026-07-19 10:00:00"}]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(agent, "2026-07-19T11:00:00+00:00")

    got, source = await agent.get_remediation_log(limit=10)
    assert source == "db+memory"  # kayıp-kuyruk görünür + kaynak dürüstçe etiketli
    assert got[0]["alert_source"] == "docker:n8n"  # in-memory taze kayıt önde
    assert got[1]["alert_source"] == "service:x"


async def test_remediation_log_no_merge_when_memory_older():
    # In-memory kayıt DB'nin en-yenisinden ESKİYSE merge edilmez (tarihsel-çift üretme).
    db_rows = [{"id": 2, "alert_source": "service:x", "action": "restart", "timestamp": "2026-07-19 12:00:00"}]
    agent = DevOpsAgent(db=_RowsDB(db_rows), interval=60)
    _mem_record(agent, "2026-07-19T09:00:00+00:00")

    got, source = await agent.get_remediation_log(limit=10)
    assert source == "db"
    assert len(got) == 1
