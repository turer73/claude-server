"""Read-only teşhis asistanı testleri (devops_agent).

Kritik garanti: teşhis yolu KOMUT ÇALIŞTIRMAZ (read-only) ve fail-silent
(alert akışını asla bozmaz).
"""

import sqlite3

import pytest

from app.core import devops_agent as da
from app.core.devops_agent import Alert, DevOpsAgent


def _alert(source="cpu"):
    return Alert(
        id="x",
        severity="critical",
        source=source,
        message="cpu sustained %95",
        value=95.0,
        threshold=90.0,
        timestamp="2026-06-19T00:00:00",
    )


@pytest.fixture
def agent():
    a = DevOpsAgent(db=None, interval=60)
    a._verify_grace = 0
    return a


async def test_diagnose_emits_diagnosis_event(agent, monkeypatch):
    captured = {}
    monkeypatch.setattr(da, "emit_event", lambda **kw: captured.update(kw))

    async def fake_ask(alert, ctx):
        return "Muhtemel kök: 2 gün önce renderhane reindex cron'u. CPU deseni eşleşiyor."

    monkeypatch.setattr(agent, "_ask_diagnosis", fake_ask)
    monkeypatch.setattr(agent, "_gather_diag_context", lambda: "- [d] renderhane/fix: reindex")

    await agent._diagnose_and_emit(_alert("cpu"))

    assert captured.get("source") == "diagnosis:cpu"
    assert "Teşhis" in captured.get("title", "")
    assert "renderhane" in captured.get("detail", "")
    assert captured.get("severity") == "warning"
    assert "KOMUT ÇALIŞTIRILMADI" in captured.get("detail", "")


async def test_diagnose_failure_is_silent(agent, monkeypatch):
    """Ollama down (None döner) → emit YOK, exception YOK."""
    calls = []
    monkeypatch.setattr(da, "emit_event", lambda **k: calls.append(k))

    async def fake_ask(a, c):
        return None

    monkeypatch.setattr(agent, "_ask_diagnosis", fake_ask)
    monkeypatch.setattr(agent, "_gather_diag_context", lambda: "x")

    await agent._diagnose_and_emit(_alert())  # raise ETMEMELİ
    assert calls == []


async def test_diagnose_never_executes_commands(agent, monkeypatch):
    """READ-ONLY garantisi: teşhis yolu shell executor'a ASLA dokunmaz."""

    def boom(*a, **k):
        raise AssertionError("teşhis bir komut çalıştırdı — read-only ihlali!")

    monkeypatch.setattr(agent._executor, "execute", boom)
    monkeypatch.setattr(da, "emit_event", lambda **k: None)

    async def fake_ask(a, c):
        return "hipotez"

    monkeypatch.setattr(agent, "_ask_diagnosis", fake_ask)
    monkeypatch.setattr(agent, "_gather_diag_context", lambda: "x")

    await agent._diagnose_and_emit(_alert())  # AssertionError gelmezse read-only doğru


def test_maybe_diagnose_disabled_does_nothing(agent):
    agent._diagnostic_enabled = False
    agent._maybe_diagnose(_alert())
    assert agent._diagnosed == set()


async def test_maybe_diagnose_once_per_incident(agent, monkeypatch):
    import asyncio

    calls = []

    async def fake_path(alert):
        calls.append(alert.source)

    monkeypatch.setattr(agent, "_diagnose_and_emit", fake_path)
    agent._maybe_diagnose(_alert("cpu"))
    agent._maybe_diagnose(_alert("cpu"))  # aynı kaynak → atla (dedup)
    await asyncio.sleep(0.02)

    assert calls == ["cpu"]
    assert "cpu" in agent._diagnosed


async def test_auto_resolve_clears_diagnosed(agent):
    agent._diagnosed.add("cpu")
    agent._active_alerts["cpu"] = _alert("cpu")
    # metrik normale döndü → resolve → diagnosed temizlenir (tekrarda yeniden teşhis)
    agent._auto_resolve({"cpu_percent": 5})
    assert "cpu" not in agent._diagnosed


def test_gather_diag_context_reads_recent_changes(agent, tmp_path):
    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, created_at TEXT);
        CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, project TEXT, task TEXT, created_at TEXT);
        INSERT INTO discoveries (project,type,title,created_at) VALUES ('renderhane','fix','reindex cron', datetime('now','-1 day'));
        INSERT INTO discoveries (project,type,title,created_at) VALUES ('old','fix','eski', datetime('now','-30 days'));
        INSERT INTO tasks_log (project,task,created_at) VALUES ('klipper','deploy', datetime('now','-2 days'));
        """
    )
    conn.commit()
    conn.close()
    agent._diag_memory_db = str(db)

    ctx = agent._gather_diag_context()
    assert "renderhane/fix: reindex cron" in ctx
    assert "klipper task: deploy" in ctx
    assert "eski" not in ctx  # 7 günden eski → dahil değil


def test_gather_diag_context_missing_db_is_safe(agent):
    agent._diag_memory_db = "/nonexistent/path/to/mem.db"
    ctx = agent._gather_diag_context()
    assert isinstance(ctx, str)  # exception değil, fallback string
