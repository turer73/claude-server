"""Local service/docker probe İYİLEŞME dalı — stuck-open alert + ikinci-kesinti sessizliği.

Repro-test (fix öncesi FAIL): probe.py'nin service ve docker döngülerinde `else:` dalı YOKTU.
PR#340 follow-up'ı bu dalı yalnız vps:* yoluna eklemişti (probe.py `_check_vps`), local yola
eklememişti. İki ayrı arıza üretiyordu:

  (1) test_local_service_recovery_resolves_db / test_local_container_recovery_resolves_db
      Kaynak düzelince DB satırı resolved=0 kalıyordu. Canlı kanıt (2026-08-07 taraması):
      docker:dozzle/n8n/uptime-kuma 2026-07-31'den, service:ollama 2026-08-01'den beri açık —
      dördü de o an sağlıklıydı.

  (2) test_second_outage_after_recovery_alerts_again  ← DAHA SİNSİ OLAN
      Yeni-alarm üretimi `source not in self._active_alerts` ile kapılı. Düzelen kaynak
      in-memory setten silinmediği için AYNI process ömründe İKİNCİ kesinti hiç alarm
      üretmiyordu — ne _store_alert ne remediate. Yalnız servis restart'ı temizliyordu.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.core.devops.models import Alert
from app.core.devops_agent import DevOpsAgent


def _mk_alert(source: str) -> Alert:
    return Alert(id=f"{source}-1", severity="critical", source=source, message="x", value=0, threshold=1, timestamp="t")


async def test_local_service_recovery_resolves_db():
    """Servis tekrar active olunca DB satırı KOŞULSUZ kapanmalı (in-memory nesneye bağlı değil)."""
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_services = ["ollama"]
    agent._critical_containers = []
    agent._active_alerts["service:ollama"] = _mk_alert("service:ollama")

    async def mock_exec(cmd, timeout=5):
        return {"stdout": "active\n", "stderr": "", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock) as resolve,
    ):
        await agent._check_services()
        await asyncio.sleep(0)

    assert "service:ollama" in {c.args[0] for c in resolve.call_args_list}
    assert "service:ollama" not in agent._active_alerts, "in-memory kayıt silinmedi -> 2. kesinti sessiz kalır"
    assert store.call_count == 0, "sağlıklı kaynak için alarm yazılmamalı"


async def test_local_container_recovery_resolves_db():
    """Container tekrar Up olunca DB satırı kapanmalı — service yoluyla simetrik."""
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_services = []
    agent._critical_containers = ["n8n"]
    agent._active_alerts["docker:n8n"] = _mk_alert("docker:n8n")

    async def mock_exec(cmd, timeout=5):
        return {"stdout": "Up 4 days (healthy)\n", "stderr": "", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock) as resolve,
    ):
        await agent._check_services()
        await asyncio.sleep(0)

    assert "docker:n8n" in {c.args[0] for c in resolve.call_args_list}
    assert "docker:n8n" not in agent._active_alerts
    assert store.call_count == 0


async def test_unhealthy_container_is_not_treated_as_recovered():
    """'Up (unhealthy)' iyileşme DEĞİL — resolve edilmemeli, alarm üretilmeli (Codex P2 dersi korunuyor)."""
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_services = []
    agent._critical_containers = ["qdrant"]

    async def mock_exec(cmd, timeout=5):
        return {"stdout": "Up 2 hours (unhealthy)\n", "stderr": "", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_remediate_container", new_callable=AsyncMock),
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock) as resolve,
    ):
        await agent._check_services()
        await asyncio.sleep(0)

    assert resolve.call_count == 0, "unhealthy 'düzeldi' sayıldı"
    assert "docker:qdrant" in {c.args[0].source for c in store.call_args_list}


async def test_second_outage_after_recovery_alerts_again():
    """down -> up -> down: İKİNCİ kesinti de alarm+remediate üretmeli (aynı process ömründe).

    Fix öncesi: ilk kesintide source _active_alerts'e giriyor, iyileşmede silinmiyor, ikinci
    kesintide `source not in self._active_alerts` False -> tamamen sessiz.
    """
    agent = DevOpsAgent(db=None, interval=60)
    agent._critical_services = []
    agent._critical_containers = ["n8n"]

    statuses = ["", "Up 1 minute (healthy)", ""]  # down, up, down

    async def mock_exec(cmd, timeout=5):
        return {"stdout": statuses[call_idx[0]], "stderr": "", "exit_code": 0}

    call_idx = [0]
    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_store_alert", new_callable=AsyncMock) as store,
        patch.object(agent, "_remediate_container", new_callable=AsyncMock) as remediate,
        patch.object(agent, "_resolve_alert_db_by_source", new_callable=AsyncMock),
    ):
        for i in range(3):
            call_idx[0] = i
            await agent._check_services()
            await asyncio.sleep(0)

    assert store.call_count == 2, f"ikinci kesinti alarm üretmedi (store={store.call_count}, beklenen 2)"
    assert remediate.call_count == 2, f"ikinci kesintide remediate çalışmadı ({remediate.call_count})"
