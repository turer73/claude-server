from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetLimits:
    daily_replies: int = 50
    daily_tokens: int = 50_000
    daily_new_threads: int = 5
    concurrent_in_flight: int = 2


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    owner_token: str
    day_utc: str
    replies: int
    estimated_tokens: int
    new_threads: int


def reserve_budget(
    conn: sqlite3.Connection,
    *,
    day_utc: str,
    estimated_tokens: int,
    is_new_thread: bool,
    limits: BudgetLimits,
    stale_after_seconds: float = 900,
    now: float | None = None,
) -> BudgetReservation | None:
    if not day_utc or estimated_tokens <= 0 or stale_after_seconds <= 0:
        raise ValueError("invalid budget request")
    if min(
        limits.daily_replies,
        limits.daily_tokens,
        limits.daily_new_threads,
        limits.concurrent_in_flight,
    ) < 0:
        raise ValueError("budget limits cannot be negative")
    current = time.time() if now is None else now
    reservation_id = secrets.token_urlsafe(24)
    owner_token = secrets.token_urlsafe(24)
    new_threads = int(is_new_thread)
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale_rows = conn.execute(
            """
            SELECT reservation_id, day_utc, replies, estimated_tokens, new_threads
            FROM autonomous_comms_budget_reservations
            WHERE state = 'active' AND updated_at <= ?
            """,
            (current - stale_after_seconds,),
        ).fetchall()
        for stale in stale_rows:
            conn.execute(
                """
                UPDATE autonomous_comms_daily_budget
                SET replies_reserved = MAX(0, replies_reserved - ?),
                    tokens_reserved = MAX(0, tokens_reserved - ?),
                    new_threads_reserved = MAX(0, new_threads_reserved - ?),
                    in_flight = MAX(0, in_flight - 1),
                    updated_at = ?
                WHERE day_utc = ?
                """,
                (int(stale[2]), int(stale[3]), int(stale[4]), current, str(stale[1])),
            )
            conn.execute(
                """
                UPDATE autonomous_comms_budget_reservations
                SET state = 'refunded', updated_at = ?
                WHERE reservation_id = ? AND state = 'active'
                """,
                (current, str(stale[0])),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO autonomous_comms_daily_budget
                (day_utc, replies_reserved, tokens_reserved, new_threads_reserved, in_flight, updated_at)
            VALUES (?, 0, 0, 0, 0, ?)
            """,
            (day_utc, current),
        )
        row = conn.execute(
            """
            SELECT replies_reserved, tokens_reserved, new_threads_reserved, in_flight
            FROM autonomous_comms_daily_budget WHERE day_utc = ?
            """,
            (day_utc,),
        ).fetchone()
        allowed = (
            int(row[0]) + 1 <= limits.daily_replies
            and int(row[1]) + estimated_tokens <= limits.daily_tokens
            and int(row[2]) + new_threads <= limits.daily_new_threads
            and int(row[3]) + 1 <= limits.concurrent_in_flight
        )
        if not allowed:
            conn.rollback()
            return None
        conn.execute(
            """
            UPDATE autonomous_comms_daily_budget
            SET replies_reserved = replies_reserved + 1,
                tokens_reserved = tokens_reserved + ?,
                new_threads_reserved = new_threads_reserved + ?,
                in_flight = in_flight + 1,
                updated_at = ?
            WHERE day_utc = ?
            """,
            (estimated_tokens, new_threads, current, day_utc),
        )
        conn.execute(
            """
            INSERT INTO autonomous_comms_budget_reservations
                (reservation_id, owner_token, day_utc, replies, estimated_tokens,
                 new_threads, state, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, 'active', ?, ?)
            """,
            (reservation_id, owner_token, day_utc, estimated_tokens, new_threads, current, current),
        )
        conn.commit()
        return BudgetReservation(reservation_id, owner_token, day_utc, 1, estimated_tokens, new_threads)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def finalize_budget(
    conn: sqlite3.Connection,
    reservation: BudgetReservation,
    *,
    success: bool,
    actual_tokens: int = 0,
    now: float | None = None,
) -> bool:
    if actual_tokens < 0 or actual_tokens > reservation.estimated_tokens:
        raise ValueError("actual_tokens must fit reserved estimate")
    current = time.time() if now is None else now
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT state FROM autonomous_comms_budget_reservations
            WHERE reservation_id = ? AND owner_token = ?
            """,
            (reservation.reservation_id, reservation.owner_token),
        ).fetchone()
        if row is None or str(row[0]) != "active":
            conn.rollback()
            return False
        if success:
            reply_refund = 0
            thread_refund = 0
            token_refund = reservation.estimated_tokens - actual_tokens
            final_state = "committed"
        else:
            reply_refund = reservation.replies
            thread_refund = reservation.new_threads
            token_refund = reservation.estimated_tokens
            final_state = "refunded"
        conn.execute(
            """
            UPDATE autonomous_comms_daily_budget
            SET replies_reserved = replies_reserved - ?,
                tokens_reserved = tokens_reserved - ?,
                new_threads_reserved = new_threads_reserved - ?,
                in_flight = in_flight - 1,
                updated_at = ?
            WHERE day_utc = ?
            """,
            (reply_refund, token_refund, thread_refund, current, reservation.day_utc),
        )
        conn.execute(
            """
            UPDATE autonomous_comms_budget_reservations
            SET state = ?, updated_at = ?
            WHERE reservation_id = ? AND owner_token = ? AND state = 'active'
            """,
            (final_state, current, reservation.reservation_id, reservation.owner_token),
        )
        conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
