"""LIVESYS-INTERV — müdahale güvenliği: provenance + reversible-set + auto-rollback.

devops_agent FAZ5 üstüne: yalnız mode=auto'da aktif. Rollback DAR (cpu-governor),
anti-flapping cooldown'lu, irreversible komutlar capture-edilmez (escalate-only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.core.devops_agent import Alert, DevOpsAgent
from app.core.provenance import build_provenance, provenance_json


def _alert(source="temperature"):
    return Alert(id="x-1", severity="critical", source=source, message="hot 90C", value=90, threshold=80, timestamp="now")


# ── provenance (saf) ──────────────────────────────────────────


def test_provenance_build_keys():
    p = build_provenance(_alert("cpu"), "auto", detected_at="2026-06-08T00:00:00+00:00")
    assert p["trigger_source"] == "cpu"
    assert p["severity"] == "critical"
    assert p["agent"] == "devops_agent"
    assert p["mode"] == "auto"
    assert p["detected_at"] == "2026-06-08T00:00:00+00:00"


def test_provenance_json_roundtrip():
    import json

    s = provenance_json(_alert(), "notify")
    d = json.loads(s)
    assert d["mode"] == "notify"
    assert d["trigger_source"] == "temperature"


# ── reversible-set (DAR) ──────────────────────────────────────


def test_reversible_kind_governor_yes_others_no():
    agent = DevOpsAgent(db=None, interval=60)
    assert agent._reversible_kind("cpufreq-set -g powersave || true") == "governor"
    assert agent._reversible_kind("echo powersave > /sys/.../scaling_governor") == "governor"
    assert agent._reversible_kind("docker system prune -f") is None  # geri-alınamaz
    assert agent._reversible_kind("systemctl restart x") is None  # geri-alınamaz
    assert agent._reversible_kind("find /tmp -delete") is None


# ── capture (aksiyon-öncesi durum) ────────────────────────────


async def test_capture_governor_stores_valid_state():
    agent = DevOpsAgent(db=None, interval=60)
    with patch.object(agent._executor, "execute", new_callable=AsyncMock, return_value={"stdout": "schedutil\n", "exit_code": 0}):
        await agent._capture_rollback("temperature", "cpufreq-set -g powersave")
    assert agent._rollback_state["temperature"]["state"] == "schedutil"


async def test_capture_rejects_injection_governor():
    # GÜVENLİK: governor okuması bozuk/enjeksiyon ise saklama (rollback yok)
    agent = DevOpsAgent(db=None, interval=60)
    with patch.object(agent._executor, "execute", new_callable=AsyncMock, return_value={"stdout": "evil; rm -rf /\n", "exit_code": 0}):
        await agent._capture_rollback("temperature", "cpufreq-set -g powersave")
    assert "temperature" not in agent._rollback_state


async def test_capture_skips_irreversible():
    agent = DevOpsAgent(db=None, interval=60)
    with patch.object(agent._executor, "execute", new_callable=AsyncMock) as ex:
        await agent._capture_rollback("disk", "docker system prune -f")
    assert "disk" not in agent._rollback_state
    ex.assert_not_called()  # reversible değil -> executor'a hiç gitme


# ── auto-rollback (verify-fail) ───────────────────────────────


async def test_rollback_runs_on_verify_fail():
    agent = DevOpsAgent(db=None, interval=60)
    agent._remediation_mode = "auto"
    agent._verify_grace = 0
    agent._rollback_state["temperature"] = {"kind": "governor", "state": "schedutil", "command": "cpufreq-set -g powersave"}
    captured: list[str] = []

    async def mock_exec(cmd, timeout=30):
        captured.append(cmd)
        # re-read (cat scaling_governor) -> hedef governor döndü (rollback DOĞRULANDI)
        if cmd.strip().startswith("cat "):
            return {"stdout": "schedutil\n", "exit_code": 0}
        return {"stdout": "ok", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_verify_remediation", new_callable=AsyncMock, return_value=False),
        patch("app.core.devops.escalation.emit_event"),
    ):
        await agent._verify_and_escalate("temperature", _alert())

    assert any("schedutil" in c for c in captured)  # önceki governor'a dönüldü
    assert "temperature" not in agent._rollback_state  # state tüketildi
    assert agent._last_rollback.get("temperature") is not None  # doğrulanmış rollback -> cooldown


async def test_rollback_not_reported_when_governor_unchanged():
    # Codex P2: rollback komutu çalışsa da governor GERİ DÖNMEDİYSE (whitelist-eksik/|| true
    # maskeleme) rolled_back=False olmalı + cooldown başlamamalı (yalan-rollback yok).
    agent = DevOpsAgent(db=None, interval=60)
    agent._rollback_state["temperature"] = {"kind": "governor", "state": "schedutil", "command": "x"}

    async def mock_exec(cmd, timeout=30):
        if cmd.strip().startswith("cat "):
            return {"stdout": "powersave\n", "exit_code": 0}  # HÂLÂ powersave -> geri DÖNMEDİ
        return {"stdout": "", "exit_code": 0}

    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec):
        rolled, res = await agent._attempt_rollback("temperature")
    assert rolled is False  # doğrulanamadı -> rolled_back RAPORLAMA
    assert "DOĞRULANAMADI" in res
    assert agent._last_rollback.get("temperature") is None  # cooldown başlamadı


async def test_rollback_false_on_executor_exception():
    # cpufreq-set whitelist'te değilse executor RAISE -> rolled_back=False (Codex P2)
    agent = DevOpsAgent(db=None, interval=60)
    agent._rollback_state["temperature"] = {"kind": "governor", "state": "schedutil", "command": "x"}
    with patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=RuntimeError("not whitelisted")):
        rolled, res = await agent._attempt_rollback("temperature")
    assert rolled is False
    assert agent._last_rollback.get("temperature") is None


async def test_no_rollback_on_verify_pass():
    agent = DevOpsAgent(db=None, interval=60)
    agent._remediation_mode = "auto"
    agent._verify_grace = 0
    agent._rollback_state["temperature"] = {"kind": "governor", "state": "schedutil", "command": "x"}

    async def mock_exec(cmd, timeout=30):
        return {"stdout": "ok", "exit_code": 0}

    with (
        patch.object(agent._executor, "execute", new_callable=AsyncMock, side_effect=mock_exec),
        patch.object(agent, "_verify_remediation", new_callable=AsyncMock, return_value=True),  # PASS
        patch("app.core.devops.escalation.emit_event"),
    ):
        await agent._verify_and_escalate("temperature", _alert())
    # verify PASS -> rollback denenmez AMA state TEMİZLENİR (surer F1: bayat-state bırakma)
    assert "temperature" not in agent._rollback_state


async def test_rollback_anti_flapping():
    import time

    agent = DevOpsAgent(db=None, interval=60)
    agent._rollback_state["temperature"] = {"kind": "governor", "state": "schedutil", "command": "x"}
    agent._last_rollback["temperature"] = time.monotonic()  # az önce rollback olmuş -> flapping
    with patch.object(agent._executor, "execute", new_callable=AsyncMock) as ex:
        rolled, res = await agent._attempt_rollback("temperature")
    assert rolled is False
    assert "flapping" in res
    ex.assert_not_called()  # cooldown'da -> komut çalıştırma


async def test_notify_mode_no_rollback_state():
    # KRİTİK: default 'notify' -> exec yok -> capture yok -> rollback yok (dormant)
    agent = DevOpsAgent(db=None, interval=60)
    assert agent._remediation_mode != "auto"  # default notify/dry_run
    with patch.object(agent._executor, "execute", new_callable=AsyncMock) as ex:
        await agent._apply_remediation(_alert(), "temperature", "Set governor", "cpufreq-set -g powersave")
    assert agent._rollback_state == {}  # notify'da hiç capture yok
    ex.assert_not_called()  # notify'da executor'a hiç gitme


# ── migration idempotent ──────────────────────────────────────


async def test_remediation_log_migration_idempotent(tmp_path):
    from app.db.database import Database

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()  # _migrate dahil
    await db._migrate()  # 2. kez -> hata YOK (idempotent)
    cur = await db.conn.execute("PRAGMA table_info(remediation_log)")
    cols = {r[1] for r in await cur.fetchall()}
    await db.close()
    assert {"rolled_back", "rollback_result", "provenance"} <= cols


def test_provenance_uses_alert_timestamp():
    # surer F2: detected_at alert.timestamp olmalı (build-time datetime.now değil)
    a = _alert()
    a.timestamp = "2026-06-08T12:00:00+00:00"
    p = build_provenance(a, "auto", detected_at=a.timestamp)
    assert p["detected_at"] == "2026-06-08T12:00:00+00:00"
