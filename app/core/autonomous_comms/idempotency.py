from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum


class ProcessingState(StrEnum):
    PROCESSING = "processing"
    SENT = "sent"
    HELD = "held"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessingClaim:
    thread_id: int
    source_note_id: int
    owner_token: str


def begin_processing(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    source_note_id: int,
    stale_after_seconds: float,
    now: float | None = None,
) -> ProcessingClaim | None:
    if thread_id <= 0 or source_note_id <= 0 or stale_after_seconds <= 0:
        raise ValueError("invalid idempotency input")
    current = time.time() if now is None else now
    token = secrets.token_urlsafe(24)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT state, updated_at
            FROM autonomous_comms_processing
            WHERE thread_id = ? AND source_note_id = ?
            """,
            (thread_id, source_note_id),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO autonomous_comms_processing
                    (thread_id, source_note_id, state, owner_token, updated_at)
                VALUES (?, ?, 'processing', ?, ?)
                """,
                (thread_id, source_note_id, token, current),
            )
        else:
            state, updated_at = str(row[0]), float(row[1])
            reclaimable = state == ProcessingState.FAILED or (
                state == ProcessingState.PROCESSING and updated_at <= current - stale_after_seconds
            )
            if not reclaimable:
                conn.rollback()
                return None
            cursor = conn.execute(
                """
                UPDATE autonomous_comms_processing
                SET state = 'processing', owner_token = ?, outgoing_note_id = NULL, updated_at = ?
                WHERE thread_id = ? AND source_note_id = ? AND state != 'sent'
                  AND (state = 'failed' OR updated_at <= ?)
                """,
                (token, current, thread_id, source_note_id, current - stale_after_seconds),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
        conn.commit()
        return ProcessingClaim(thread_id, source_note_id, token)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def finish_processing(
    conn: sqlite3.Connection,
    claim: ProcessingClaim,
    *,
    state: ProcessingState,
    outgoing_note_id: int | None = None,
    now: float | None = None,
) -> bool:
    if state is ProcessingState.PROCESSING:
        raise ValueError("finish state cannot be processing")
    if state is ProcessingState.SENT and (outgoing_note_id is None or outgoing_note_id <= 0):
        raise ValueError("sent state requires outgoing_note_id")
    current = time.time() if now is None else now
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE autonomous_comms_processing
            SET state = ?, outgoing_note_id = ?, updated_at = ?
            WHERE thread_id = ? AND source_note_id = ?
              AND state = 'processing' AND owner_token = ?
            """,
            (
                state.value,
                outgoing_note_id,
                current,
                claim.thread_id,
                claim.source_note_id,
                claim.owner_token,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
