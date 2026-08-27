from __future__ import annotations

import sqlite3

import pytest

from app.core.autonomous_comms.budget import BudgetLimits
from app.core.autonomous_comms.claims import acquire_claim
from app.core.autonomous_comms.dialogue import DialogueFailure, DialogueSuccess, DialogueTurn
from app.core.autonomous_comms.pipeline import RuntimeConfig, process_note
from app.core.autonomous_comms.promotion import record_promotion_metrics, set_human_approval
from app.core.autonomous_comms.schema import ensure_schema


class FakeProducer:
    def __init__(self, reply: str = "Kısa ve güvenli bir değerlendirme paylaşabilirim.", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls = 0

    def produce(self, turns: list[DialogueTurn]) -> DialogueSuccess | DialogueFailure:
        self.calls += 1
        if self.fail:
            return DialogueFailure("provider_error")
        return DialogueSuccess(self.reply, len(turns))


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_device TEXT NOT NULL,
            to_device TEXT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            read INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            verified INTEGER DEFAULT 0,
            thread_id INTEGER,
            reply_to INTEGER,
            hop_count INTEGER DEFAULT 0,
            msg_type TEXT DEFAULT 'legacy',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE autonomous_comms_halt (
            id INTEGER PRIMARY KEY CHECK (id = 1), active INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute("INSERT INTO autonomous_comms_halt(id, active) VALUES (1, 0)")
    ensure_schema(connection)
    return connection


def _note(
    connection: sqlite3.Connection,
    *,
    msg_type: str = "dialogue",
    verified: int = 1,
    content: str = "Yeni durumu değerlendirir misin?",
    thread_id: int | None = None,
    reply_to: int | None = None,
    hop_count: int = 0,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO notes
            (from_device, to_device, title, content, msg_type, verified,
             thread_id, reply_to, hop_count)
        VALUES ('agent-a', 'klipper', 'Durum', ?, ?, ?, ?, ?, ?)
        """,
        (content, msg_type, verified, thread_id, reply_to, hop_count),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _promote(connection: sqlite3.Connection, now: float = 1_000) -> None:
    set_human_approval(connection, approved=True, approved_by="admin", now=now)
    record_promotion_metrics(
        connection,
        reviewed=100,
        routing_correct=100,
        accepted=95,
        generation_total=100,
        now=now,
    )


def _active_config(**changes: object) -> RuntimeConfig:
    values: dict[str, object] = {"operator_enabled": True}
    values.update(changes)
    return RuntimeConfig(**values)


def test_shadow_generates_candidate_but_never_sends(conn: sqlite3.Connection) -> None:
    source_id = _note(conn)
    producer = FakeProducer()
    result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=RuntimeConfig(operator_enabled=False),
        producer=producer,
        now=1_000,
    )
    assert result.reason == "shadow_candidate_ready"
    assert result.shadow_reply_generated is True
    assert result.outgoing_note_id is None
    assert producer.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM autonomous_comms_processing").fetchone()[0] == "held"


def test_active_send_derives_authoritative_reply_fields(conn: sqlite3.Connection) -> None:
    source_id = _note(conn)
    _promote(conn)
    result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=_active_config(),
        producer=FakeProducer("Bu durum için ek kanıt bekliyorum."),
        now=1_000,
    )
    assert result.reason == "active_sent"
    row = conn.execute(
        """
        SELECT from_device, to_device, msg_type, verified, thread_id, reply_to, hop_count
        FROM notes WHERE id = ?
        """,
        (result.outgoing_note_id,),
    ).fetchone()
    assert tuple(row) == ("klipper-autonomous", "agent-a", "dialogue", 1, source_id, source_id, 1)
    audited = {
        row[0]
        for row in conn.execute(
            "SELECT decision FROM autonomous_comms_decision_audit WHERE source_note_id = ?",
            (source_id,),
        ).fetchall()
    }
    assert {"promotion", "route", "claim", "idempotency", "budget", "generation", "guard", "send"} <= audited


def test_duplicate_retry_returns_same_outgoing_note(conn: sqlite3.Connection) -> None:
    source_id = _note(conn)
    _promote(conn)
    first = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=_active_config(),
        producer=FakeProducer("İlk güvenli yanıt."),
        now=1_000,
    )
    second_producer = FakeProducer("İkinci farklı yanıt.")
    second = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=_active_config(),
        producer=second_producer,
        now=1_001,
    )
    assert second.duplicate is True
    assert second.outgoing_note_id == first.outgoing_note_id
    assert second_producer.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("msg_type", "verified", "expected"),
    [
        ("legacy", 1, "legacy_dispatch_equivalent"),
        ("dispatch", 1, "dispatch_hold"),
        ("forged", 1, "unknown_message_type"),
        ("dialogue", 0, "unknown_message_type"),
    ],
)
def test_legacy_dispatch_unknown_or_unverified_never_generate(
    conn: sqlite3.Connection,
    msg_type: str,
    verified: int,
    expected: str,
) -> None:
    source_id = _note(conn, msg_type=msg_type, verified=verified)
    _promote(conn)
    producer = FakeProducer()
    result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=_active_config(),
        producer=producer,
        now=1_000,
    )
    assert result.reason == expected
    assert producer.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_kill_switch_active_or_missing_fails_closed(conn: sqlite3.Connection) -> None:
    source_id = _note(conn)
    _promote(conn)
    conn.execute("UPDATE autonomous_comms_halt SET active = 1 WHERE id = 1")
    conn.commit()
    producer = FakeProducer()
    assert (
        process_note(
            conn,
            trusted_sender="klipper-autonomous",
            source_note_id=source_id,
            config=_active_config(),
            producer=producer,
            now=1_000,
        ).reason
        == "kill_switch_active"
    )
    conn.execute("DROP TABLE autonomous_comms_halt")
    conn.commit()
    assert (
        process_note(
            conn,
            trusted_sender="klipper-autonomous",
            source_note_id=source_id,
            config=_active_config(),
            producer=producer,
            now=1_001,
        ).reason
        == "kill_switch_active"
    )
    assert producer.calls == 0


def test_closed_thread_and_hop_boundary_reject(conn: sqlite3.Connection) -> None:
    closed_id = _note(conn)
    hop_id = _note(conn, hop_count=3)
    conn.execute(
        "INSERT INTO autonomous_comms_thread_state(thread_id, state, updated_at) VALUES (?, 'closed', 1000)",
        (closed_id,),
    )
    conn.commit()
    _promote(conn)
    producer = FakeProducer()
    closed = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=closed_id,
        config=_active_config(max_hops=3),
        producer=producer,
        now=1_000,
    )
    hop = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=hop_id,
        config=_active_config(max_hops=3),
        producer=producer,
        now=1_000,
    )
    assert closed.reason == "terminal_thread_state"
    assert hop.reason == "missing_parent"
    assert producer.calls == 0


def test_invalid_parent_thread_rejected(conn: sqlite3.Connection) -> None:
    root_a = _note(conn)
    root_b = _note(conn)
    source_id = _note(conn, thread_id=root_a, reply_to=root_b, hop_count=1)
    result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_id,
        config=_active_config(),
        producer=FakeProducer(),
        now=1_000,
    )
    assert result.reason == "parent_thread_mismatch"


def test_claim_contention_budget_denial_generation_failure_and_loop_block(conn: sqlite3.Connection) -> None:
    source_claim = _note(conn)
    source_budget = _note(conn)
    source_failure = _note(conn)
    source_loop = _note(conn, content="Aynı kısa yanıt.")
    _promote(conn)
    assert acquire_claim(conn, thread_id=source_claim, owner_id="other", lease_seconds=60, now=1_000)
    claim_result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_claim,
        config=_active_config(),
        producer=FakeProducer(),
        now=1_000,
    )
    zero_budget = BudgetLimits(0, 0, 0, 0)
    budget_producer = FakeProducer()
    budget_result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_budget,
        config=_active_config(budget_limits=zero_budget),
        producer=budget_producer,
        now=1_000,
    )
    failure_result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_failure,
        config=_active_config(),
        producer=FakeProducer(fail=True),
        now=1_000,
    )
    loop_result = process_note(
        conn,
        trusted_sender="klipper-autonomous",
        source_note_id=source_loop,
        config=_active_config(),
        producer=FakeProducer("Aynı kısa yanıt!"),
        now=1_000,
    )
    assert claim_result.reason == "thread_claim_busy"
    assert budget_result.reason == "budget_denied"
    assert budget_producer.calls == 0
    assert failure_result.reason == "generation_failed:provider_error"
    assert loop_result.reason.startswith("loop_blocked:")
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 4
