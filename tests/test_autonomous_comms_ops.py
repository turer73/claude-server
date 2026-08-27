from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.api.memory as memory_api
import app.api.memory.comms as comms_api
from app.api.memory.comms import PromotionApproval, ShadowReview
from app.core.autonomous_comms.budget import BudgetLimits
from app.core.autonomous_comms.dialogue import DialogueSuccess, DialogueTurn
from app.core.autonomous_comms.pipeline import RuntimeConfig, process_note
from app.core.autonomous_comms.schema import ensure_schema
from automation.autonomous_comms_poller import process_batch, runtime_config


class FakeProducer:
    def produce(self, turns: list[DialogueTurn]) -> DialogueSuccess:
        return DialogueSuccess("Güvenli bir değerlendirme paylaşabilirim.", len(turns))


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "ops.db")
    conn = _connect(path)
    conn.execute(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_device TEXT NOT NULL, to_device TEXT, title TEXT NOT NULL,
            content TEXT NOT NULL, read INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active', verified INTEGER DEFAULT 1,
            thread_id INTEGER, reply_to INTEGER, hop_count INTEGER DEFAULT 0,
            msg_type TEXT DEFAULT 'dialogue', created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("CREATE TABLE autonomous_comms_halt(id INTEGER PRIMARY KEY, active INTEGER NOT NULL)")
    conn.execute("INSERT INTO autonomous_comms_halt VALUES (1, 0)")
    ensure_schema(conn)
    conn.close()
    return path


def _insert_note(path: str) -> int:
    conn = _connect(path)
    cursor = conn.execute(
        """
        INSERT INTO notes(from_device, to_device, title, content, msg_type, verified)
        VALUES ('agent-a', 'klipper', 'Status', 'Durumu değerlendir.', 'dialogue', 1)
        """
    )
    conn.commit()
    note_id = int(cursor.lastrowid)
    conn.close()
    return note_id


@pytest.mark.asyncio
async def test_shadow_review_api_is_unique_and_updates_metrics(db_path: str, monkeypatch) -> None:
    source_id = _insert_note(db_path)
    conn = _connect(db_path)
    shadow = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=RuntimeConfig(operator_enabled=False),
        producer=FakeProducer(),
        now=1_000,
    )
    conn.close()
    monkeypatch.setattr(comms_api, "get_db", lambda: _connect(db_path))
    review = ShadowReview(
        correlation_id=shadow.correlation_id,
        routing_correct=True,
        accepted=True,
        critical_violation=False,
    )
    candidates = await comms_api.list_shadow_candidates()
    assert candidates["candidates"][0]["correlation_id"] == shadow.correlation_id
    assert candidates["candidates"][0]["reply_text"] == "Güvenli bir değerlendirme paylaşabilirim."
    assert (await comms_api.review_shadow_candidate(review))["status"] == "recorded"
    with pytest.raises(HTTPException) as duplicate:
        await comms_api.review_shadow_candidate(review)
    assert duplicate.value.status_code == 409
    assert (await comms_api.list_shadow_candidates())["candidates"] == []
    conn = _connect(db_path)
    metrics = conn.execute(
        """
        SELECT reviewed_count, routing_correct_count, accepted_count
        FROM autonomous_comms_promotion_metrics WHERE singleton = 1
        """
    ).fetchone()
    assert tuple(metrics) == (1, 1, 1)
    assert (
        conn.execute(
            """
        SELECT COUNT(*) FROM autonomous_comms_decision_audit
        WHERE correlation_id = ? AND decision = 'human_review'
        """,
            (shadow.correlation_id,),
        ).fetchone()[0]
        == 1
    )
    conn.close()


@pytest.mark.asyncio
async def test_promotion_status_and_approval_api(db_path: str, monkeypatch) -> None:
    monkeypatch.setattr(comms_api, "get_db", lambda: _connect(db_path))
    monkeypatch.setattr(comms_api, "read_env_var", lambda _name: "0")
    status = await comms_api.promotion_status()
    assert status["mode"] == "shadow"
    assert status["operator_enabled"] is False
    assert status["canary_enabled"] is False
    assert status["advisory_only"] is True
    assert (await comms_api.update_promotion_approval(PromotionApproval(approved=True)))["approved"] is True
    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM autonomous_comms_decision_audit WHERE decision = 'promotion_admin'").fetchone()[0] == 1
    conn.close()


def test_canary_runtime_is_hard_capped(monkeypatch) -> None:
    values = {
        "AUTONOMOUS_COMMS_ACTIVE": "1",
        "AUTONOMOUS_COMMS_CANARY_ACTIVE": "1",
        "AUTONOMOUS_COMMS_DAILY_REPLIES": "500",
        "AUTONOMOUS_COMMS_DAILY_TOKENS": "500000",
        "AUTONOMOUS_COMMS_DAILY_NEW_THREADS": "50",
        "AUTONOMOUS_COMMS_IN_FLIGHT": "20",
    }
    monkeypatch.setattr(
        "automation.autonomous_comms_poller.read_env_var",
        lambda name: values.get(name),
    )
    config = runtime_config()
    assert config.operator_enabled is True
    assert config.canary_enabled is True
    assert config.budget_limits == BudgetLimits(5, 5_000, 1, 1)


def test_autonomous_credential_cannot_self_approve(monkeypatch) -> None:
    monkeypatch.setattr(memory_api, "MEMORY_API_KEY", "master")
    monkeypatch.setattr(memory_api, "MEMORY_API_KEY_ADMIN", "admin")
    monkeypatch.setattr(memory_api, "MEMORY_API_KEY_AUTONOMOUS", "autonomous")
    with pytest.raises(HTTPException) as denied:
        memory_api.verify_admin_key("autonomous")
    assert denied.value.status_code == 401
    memory_api.verify_admin_key("admin")


def test_poller_batch_advances_terminal_shadow_but_retries_transient_budget(db_path: str) -> None:
    shadow_id = _insert_note(db_path)
    conn = _connect(db_path)
    next_seen, results = process_batch(
        conn,
        notes=[{"id": shadow_id}],
        device="klipper-autonomous",
        last_seen=0,
        producer=FakeProducer(),
        config=RuntimeConfig(operator_enabled=False),
    )
    assert next_seen == shadow_id
    assert results[0]["reason"] == "shadow_candidate_ready"
    transient_id = _insert_note(db_path)
    next_seen, results = process_batch(
        conn,
        notes=[{"id": transient_id}],
        device="klipper-autonomous",
        last_seen=shadow_id,
        producer=FakeProducer(),
        config=RuntimeConfig(
            operator_enabled=False,
            budget_limits=BudgetLimits(0, 0, 0, 0),
        ),
    )
    assert next_seen == shadow_id
    assert results[0]["reason"] == "budget_denied"
    conn.close()


def test_note_poller_runs_phase_c_with_project_venv_and_fails_safe_if_missing() -> None:
    script = (Path(__file__).parents[1] / "automation" / "note-poller.sh").read_text(encoding="utf-8")
    assert 'PHASE_C_PYTHON="${PHASE_C_PYTHON:-/opt/linux-ai-server/venv/bin/python}"' in script
    assert 'if [ ! -x "$PHASE_C_PYTHON" ]; then' in script
    assert '"$PHASE_C_PYTHON" /opt/linux-ai-server/automation/autonomous_comms_poller.py' in script
    assert "spawned_max_id=$last_seen" in script
