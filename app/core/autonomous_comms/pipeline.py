from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.attention_router import create_work_item_sync
from app.core.autonomous_comms.audit import append_audit
from app.core.autonomous_comms.budget import BudgetLimits, BudgetReservation, finalize_budget, reserve_budget
from app.core.autonomous_comms.claims import ThreadClaim, acquire_claim, release_claim
from app.core.autonomous_comms.dialogue import DialogueFailure, DialogueProducer, DialogueSuccess, DialogueTurn
from app.core.autonomous_comms.idempotency import (
    ProcessingClaim,
    ProcessingState,
    begin_processing,
    finish_processing,
)
from app.core.autonomous_comms.loop_guard import detect_loop
from app.core.autonomous_comms.models import GateInput, MessageType, NoteFacts, RouteVerdict, ThreadState
from app.core.autonomous_comms.promotion import evaluate_promotion, record_promotion_metrics
from app.core.autonomous_comms.router import route
from app.core.autonomous_comms.schema import ensure_schema
from app.core.privacy import redact


@dataclass(frozen=True)
class RuntimeConfig:
    operator_enabled: bool = False
    max_hops: int = 3
    claim_lease_seconds: float = 60
    stale_processing_seconds: float = 300
    stale_budget_seconds: float = 900
    context_turns: int = 8
    estimated_reply_tokens: int = 384
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)


@dataclass(frozen=True)
class ProcessResult:
    verdict: RouteVerdict
    reason: str
    correlation_id: str
    thread_id: int | None = None
    outgoing_note_id: int | None = None
    shadow_reply_generated: bool = False
    duplicate: bool = False


def _row_dict(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    return {str(column[0]): row[index] for index, column in enumerate(cursor.description or ())}


def _load_note(conn: sqlite3.Connection, note_id: int) -> dict[str, object] | None:
    cursor = conn.execute(
        """
        SELECT id, from_device, to_device, title, content, status, verified,
               msg_type, thread_id, reply_to, hop_count
        FROM notes WHERE id = ?
        """,
        (note_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, tuple(row))


def _effective_root(note: Mapping[str, object]) -> int:
    return int(note.get("thread_id") or note["id"])


def _resolve_source(
    conn: sqlite3.Connection,
    source: Mapping[str, object],
) -> tuple[int, int, str | None]:
    source_id = int(source["id"])
    root_id = _effective_root(source)
    hop_count = int(source.get("hop_count") or 0)
    if hop_count < 0:
        return root_id, hop_count, "negative_hop_count"
    if root_id != source_id:
        root = _load_note(conn, root_id)
        if root is None or _effective_root(root) != root_id or int(root.get("hop_count") or 0) != 0:
            return root_id, hop_count, "invalid_thread_root"
    reply_to = source.get("reply_to")
    if reply_to is None:
        if root_id != source_id or hop_count != 0:
            return root_id, hop_count, "missing_parent"
        return root_id, hop_count, None
    parent = _load_note(conn, int(reply_to))
    if parent is None:
        return root_id, hop_count, "parent_not_found"
    if _effective_root(parent) != root_id:
        return root_id, hop_count, "parent_thread_mismatch"
    if hop_count != int(parent.get("hop_count") or 0) + 1:
        return root_id, hop_count, "non_monotonic_hop"
    return root_id, hop_count, None


def _thread_state(conn: sqlite3.Connection, thread_id: int) -> ThreadState:
    row = conn.execute(
        "SELECT state FROM autonomous_comms_thread_state WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    raw = "open" if row is None else str(row[0])
    try:
        return ThreadState(raw)
    except ValueError:
        return ThreadState.UNKNOWN


def _kill_switch_active(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT active FROM autonomous_comms_halt WHERE id = 1").fetchone()
        return row is None or int(row[0]) != 0
    except sqlite3.Error:
        return True


def _message_type(source: Mapping[str, object]) -> MessageType:
    try:
        return MessageType(str(source.get("msg_type") or "unknown"))
    except ValueError:
        return MessageType.UNKNOWN


def _context(conn: sqlite3.Connection, thread_id: int, limit: int) -> tuple[list[DialogueTurn], list[str]]:
    rows = conn.execute(
        """
        SELECT from_device, content FROM notes
        WHERE id = ? OR thread_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (thread_id, thread_id, limit),
    ).fetchall()
    chronological = list(reversed(rows))
    turns = [DialogueTurn("agent", str(row[1])) for row in chronological]
    contents = [str(row[1]) for row in chronological]
    return turns, contents


def _audit(
    conn: sqlite3.Connection,
    *,
    decision: str,
    reason: str,
    correlation_id: str,
    thread_id: int | None,
    source_note_id: int,
    metadata: Mapping[str, object] | None = None,
) -> None:
    append_audit(
        conn,
        decision=decision,
        reason=reason,
        correlation_id=correlation_id,
        thread_id=thread_id,
        source_note_id=source_note_id,
        idempotency_key=f"{thread_id}:{source_note_id}",
        metadata=metadata,
    )


def _work_item(
    conn: sqlite3.Connection,
    *,
    outcome: str,
    reason: str,
    correlation_id: str,
    thread_id: int | None,
    source_note_id: int,
) -> None:
    create_work_item_sync(
        conn,
        event_type=f"autonomous_comms:{outcome}",
        title=f"Autonomous comms {outcome}: {reason}"[:200],
        payload={
            "reason": reason,
            "correlation_id": correlation_id,
            "thread_id": thread_id,
            "source_note_id": source_note_id,
        },
        created_by="autonomous_comms",
    )


def _atomic_send(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, object],
    trusted_sender: str,
    thread_id: int,
    reply: str,
    processing: ProcessingClaim,
    now: float,
) -> int | None:
    source_id = int(source["id"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_source = _load_note(conn, source_id)
        if current_source is None:
            conn.rollback()
            return None
        current_root, current_hop, error = _resolve_source(conn, current_source)
        if error or current_root != thread_id:
            conn.rollback()
            return None
        processing_row = conn.execute(
            """
            SELECT state, owner_token FROM autonomous_comms_processing
            WHERE thread_id = ? AND source_note_id = ?
            """,
            (thread_id, source_id),
        ).fetchone()
        if processing_row is None or tuple(processing_row) != ("processing", processing.owner_token):
            conn.rollback()
            return None
        cursor = conn.execute(
            """
            INSERT INTO notes
                (from_device, to_device, title, content, status, verified,
                 msg_type, thread_id, reply_to, hop_count)
            VALUES (?, ?, ?, ?, 'active', 1, 'dialogue', ?, ?, ?)
            """,
            (
                trusted_sender,
                str(current_source["from_device"]),
                f"Re: {str(current_source['title'])[:180]}",
                reply,
                thread_id,
                source_id,
                current_hop + 1,
            ),
        )
        outgoing_id = int(cursor.lastrowid)
        updated = conn.execute(
            """
            UPDATE autonomous_comms_processing
            SET state = 'sent', outgoing_note_id = ?, updated_at = ?
            WHERE thread_id = ? AND source_note_id = ?
              AND state = 'processing' AND owner_token = ?
            """,
            (outgoing_id, now, thread_id, source_id, processing.owner_token),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return outgoing_id
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _store_shadow_candidate(
    conn: sqlite3.Connection,
    *,
    correlation_id: str,
    thread_id: int,
    source_note_id: int,
    reply: str,
    processing: ProcessingClaim,
    now: float,
) -> bool:
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT state, owner_token FROM autonomous_comms_processing
            WHERE thread_id = ? AND source_note_id = ?
            """,
            (thread_id, source_note_id),
        ).fetchone()
        if row is None or tuple(row) != ("processing", processing.owner_token):
            conn.rollback()
            return False
        conn.execute(
            """
            INSERT INTO autonomous_comms_shadow_candidates
                (correlation_id, thread_id, source_note_id, reply_text, state, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (correlation_id, thread_id, source_note_id, reply, now),
        )
        updated = conn.execute(
            """
            UPDATE autonomous_comms_processing
            SET state = 'held', updated_at = ?
            WHERE thread_id = ? AND source_note_id = ?
              AND state = 'processing' AND owner_token = ?
            """,
            (now, thread_id, source_note_id, processing.owner_token),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def process_note(
    conn: sqlite3.Connection,
    *,
    trusted_sender: str,
    source_note_id: int,
    config: RuntimeConfig,
    producer: DialogueProducer,
    now: float | None = None,
) -> ProcessResult:
    if not trusted_sender.strip() or source_note_id <= 0:
        raise ValueError("trusted sender and source note are required")
    current = time.time() if now is None else now
    correlation_id = uuid.uuid4().hex
    ensure_schema(conn)
    source = _load_note(conn, source_note_id)
    if source is None:
        return ProcessResult(RouteVerdict.REJECT, "source_note_not_found", correlation_id)
    thread_id, hop_count, thread_error = _resolve_source(conn, source)
    if thread_error:
        _work_item(
            conn,
            outcome="blocked",
            reason=thread_error,
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        return ProcessResult(RouteVerdict.REJECT, thread_error, correlation_id, thread_id)

    halt = _kill_switch_active(conn)
    promotion = evaluate_promotion(conn, operator_enabled=config.operator_enabled, now=current)
    msg_type = _message_type(source)
    if msg_type is MessageType.DIALOGUE and int(source.get("verified") or 0) != 1:
        msg_type = MessageType.UNKNOWN
    decision = route(
        GateInput(
            message_type=msg_type,
            note_facts=NoteFacts(_thread_state(conn, thread_id), hop_count, config.max_hops),
            kill_switch_active=halt,
            promotion_active=promotion.active,
        )
    )
    _audit(
        conn,
        decision="promotion",
        reason="active" if promotion.active else "shadow",
        correlation_id=correlation_id,
        thread_id=thread_id,
        source_note_id=source_note_id,
        metadata={"blocking_reasons": list(promotion.reasons)},
    )
    _audit(
        conn,
        decision="route",
        reason=decision.reason,
        correlation_id=correlation_id,
        thread_id=thread_id,
        source_note_id=source_note_id,
        metadata={"verdict": decision.verdict.value, "hop_count": hop_count},
    )

    shadow_generation = decision.verdict is RouteVerdict.HOLD and decision.reason == "inactive_promotion_hold"
    if decision.verdict is RouteVerdict.REJECT or (decision.verdict is RouteVerdict.HOLD and not shadow_generation):
        outcome = "blocked" if decision.verdict is RouteVerdict.REJECT else "held"
        _work_item(
            conn,
            outcome=outcome,
            reason=decision.reason,
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        return ProcessResult(decision.verdict, decision.reason, correlation_id, thread_id)

    claim: ThreadClaim | None = None
    processing: ProcessingClaim | None = None
    reservation: BudgetReservation | None = None
    budget_finalized = False
    try:
        claim = acquire_claim(
            conn,
            thread_id=thread_id,
            owner_id=f"{trusted_sender}:{correlation_id}",
            lease_seconds=config.claim_lease_seconds,
            now=current,
        )
        if claim is None:
            reason = "thread_claim_busy"
            _audit(
                conn,
                decision="claim",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            _work_item(
                conn,
                outcome="blocked",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(RouteVerdict.REJECT, reason, correlation_id, thread_id)
        _audit(
            conn,
            decision="claim",
            reason="acquired",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        processing = begin_processing(
            conn,
            thread_id=thread_id,
            source_note_id=source_note_id,
            stale_after_seconds=config.stale_processing_seconds,
            now=current,
        )
        if processing is None:
            existing = conn.execute(
                """
                SELECT state, outgoing_note_id FROM autonomous_comms_processing
                WHERE thread_id = ? AND source_note_id = ?
                """,
                (thread_id, source_note_id),
            ).fetchone()
            outgoing = None if existing is None or existing[1] is None else int(existing[1])
            state = "unknown" if existing is None else str(existing[0])
            _audit(
                conn,
                decision="idempotency",
                reason=f"duplicate_{state}",
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(
                decision.verdict,
                f"duplicate_{state}",
                correlation_id,
                thread_id,
                outgoing_note_id=outgoing,
                duplicate=True,
            )
        _audit(
            conn,
            decision="idempotency",
            reason="processing_started",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        reservation = reserve_budget(
            conn,
            day_utc=time.strftime("%Y-%m-%d", time.gmtime(current)),
            estimated_tokens=config.estimated_reply_tokens,
            is_new_thread=hop_count == 0,
            limits=config.budget_limits,
            stale_after_seconds=config.stale_budget_seconds,
            now=current,
        )
        if reservation is None:
            finish_processing(conn, processing, state=ProcessingState.FAILED, now=current)
            reason = "budget_denied"
            _audit(
                conn,
                decision="budget",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            _work_item(
                conn,
                outcome="blocked",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(RouteVerdict.REJECT, reason, correlation_id, thread_id)
        _audit(
            conn,
            decision="budget",
            reason="reserved",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
            metadata={"estimated_tokens": config.estimated_reply_tokens},
        )
        turns, recent_contents = _context(conn, thread_id, config.context_turns)
        generated = producer.produce(turns)
        if isinstance(generated, DialogueFailure):
            _audit(
                conn,
                decision="generation",
                reason=generated.reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            record_promotion_metrics(conn, generation_total=1, generation_failures=1, now=current)
            finish_processing(conn, processing, state=ProcessingState.FAILED, now=current)
            finalize_budget(conn, reservation, success=False, now=current)
            budget_finalized = True
            reason = f"generation_failed:{generated.reason}"
            _work_item(
                conn,
                outcome="failed",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(RouteVerdict.REJECT, reason, correlation_id, thread_id)
        if not isinstance(generated, DialogueSuccess):
            raise TypeError("producer returned an unsupported result")
        reply, redacted_labels = redact(generated.reply)
        reply = (reply or "").strip()
        if not reply:
            raise ValueError("reply empty after redaction")
        _audit(
            conn,
            decision="generation",
            reason="validated",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
            metadata={"output_chars": len(reply), "redaction_count": len(redacted_labels)},
        )
        loop = detect_loop(recent_contents, reply, max_turns=config.context_turns)
        if loop.repeated or loop.ping_pong:
            _audit(
                conn,
                decision="guard",
                reason=loop.reason or "loop_blocked",
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            record_promotion_metrics(conn, generation_total=1, loop_blocks=1, now=current)
            finish_processing(conn, processing, state=ProcessingState.FAILED, now=current)
            finalize_budget(conn, reservation, success=False, now=current)
            budget_finalized = True
            reason = f"loop_blocked:{loop.reason}"
            _work_item(
                conn,
                outcome="blocked",
                reason=reason,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(RouteVerdict.REJECT, reason, correlation_id, thread_id)
        _audit(
            conn,
            decision="guard",
            reason="passed",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        actual_tokens = min(config.estimated_reply_tokens, max(1, (len(reply) + 3) // 4))
        record_promotion_metrics(conn, generation_total=1, now=current)
        if shadow_generation:
            finalize_budget(conn, reservation, success=True, actual_tokens=actual_tokens, now=current)
            budget_finalized = True
            if not _store_shadow_candidate(
                conn,
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
                reply=reply,
                processing=processing,
                now=current,
            ):
                raise RuntimeError("shadow candidate precondition failed")
            _work_item(
                conn,
                outcome="held",
                reason="shadow_candidate_ready",
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            _audit(
                conn,
                decision="send",
                reason="shadow_no_send",
                correlation_id=correlation_id,
                thread_id=thread_id,
                source_note_id=source_note_id,
            )
            return ProcessResult(
                RouteVerdict.HOLD,
                "shadow_candidate_ready",
                correlation_id,
                thread_id,
                shadow_reply_generated=True,
            )
        finalize_budget(conn, reservation, success=True, actual_tokens=actual_tokens, now=current)
        budget_finalized = True
        outgoing_id = _atomic_send(
            conn,
            source=source,
            trusted_sender=trusted_sender,
            thread_id=thread_id,
            reply=reply,
            processing=processing,
            now=current,
        )
        if outgoing_id is None:
            finish_processing(conn, processing, state=ProcessingState.FAILED, now=current)
            raise RuntimeError("atomic send precondition failed")
        _audit(
            conn,
            decision="send",
            reason="active_sent",
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
            metadata={"outgoing_note_id": outgoing_id},
        )
        return ProcessResult(RouteVerdict.DIALOGUE, "active_sent", correlation_id, thread_id, outgoing_id)
    except Exception as exc:
        if reservation is not None and not budget_finalized:
            finalize_budget(conn, reservation, success=False, now=current)
        if processing is not None:
            finish_processing(conn, processing, state=ProcessingState.FAILED, now=current)
        reason = f"pipeline_failed:{type(exc).__name__}"
        _work_item(
            conn,
            outcome="failed",
            reason=reason,
            correlation_id=correlation_id,
            thread_id=thread_id,
            source_note_id=source_note_id,
        )
        return ProcessResult(RouteVerdict.REJECT, reason, correlation_id, thread_id)
    finally:
        if claim is not None:
            release_claim(conn, claim)
