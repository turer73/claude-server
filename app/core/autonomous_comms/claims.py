from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ThreadClaim:
    thread_id: int
    owner_id: str
    lease_token: str
    leased_until: float


def acquire_claim(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    owner_id: str,
    lease_seconds: float,
    now: float | None = None,
) -> ThreadClaim | None:
    if thread_id <= 0 or not owner_id.strip() or lease_seconds <= 0:
        raise ValueError("invalid claim input")
    current = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    leased_until = current + lease_seconds
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT leased_until FROM autonomous_comms_thread_claims WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is not None and float(row[0]) > current:
            conn.rollback()
            return None
        conn.execute(
            """
            INSERT INTO autonomous_comms_thread_claims
                (thread_id, owner_id, lease_token, leased_until, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                lease_token = excluded.lease_token,
                leased_until = excluded.leased_until,
                updated_at = excluded.updated_at
            WHERE autonomous_comms_thread_claims.leased_until <= ?
            """,
            (thread_id, owner_id, token, leased_until, current, current),
        )
        conn.commit()
        return ThreadClaim(thread_id, owner_id, token, leased_until)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def renew_claim(
    conn: sqlite3.Connection,
    claim: ThreadClaim,
    *,
    lease_seconds: float,
    now: float | None = None,
) -> ThreadClaim | None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    current = time.time() if now is None else now
    leased_until = current + lease_seconds
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE autonomous_comms_thread_claims
            SET leased_until = ?, updated_at = ?
            WHERE thread_id = ? AND owner_id = ? AND lease_token = ? AND leased_until > ?
            """,
            (leased_until, current, claim.thread_id, claim.owner_id, claim.lease_token, current),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
        return ThreadClaim(claim.thread_id, claim.owner_id, claim.lease_token, leased_until)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def release_claim(conn: sqlite3.Connection, claim: ThreadClaim) -> bool:
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            DELETE FROM autonomous_comms_thread_claims
            WHERE thread_id = ? AND owner_id = ? AND lease_token = ?
            """,
            (claim.thread_id, claim.owner_id, claim.lease_token),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
