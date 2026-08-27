from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionCriteria:
    min_reviewed_samples: int = 100
    min_routing_precision: float = 0.95
    min_accepted_precision: float = 0.90
    max_generation_failure_rate: float = 0.05
    max_loop_block_rate: float = 0.10
    max_approval_age_seconds: float = 7 * 24 * 3600
    max_metrics_age_seconds: float = 24 * 3600


@dataclass(frozen=True)
class PromotionDecision:
    active: bool
    reasons: tuple[str, ...]
    profile: str = "standard"


def evaluate_promotion(
    conn: sqlite3.Connection,
    *,
    operator_enabled: bool,
    canary_enabled: bool = False,
    criteria: PromotionCriteria = PromotionCriteria(),
    now: float | None = None,
) -> PromotionDecision:
    if not operator_enabled:
        return PromotionDecision(False, ("operator_config_off",), "canary" if canary_enabled else "standard")
    current = time.time() if now is None else now
    reasons: list[str] = []
    try:
        approval = conn.execute(
            """
            SELECT approved, approved_by, approved_at
            FROM autonomous_comms_promotion WHERE singleton = 1
            """
        ).fetchone()
        if approval is None:
            reasons.append("approval_missing")
        else:
            approved, approved_by, approved_at = approval
            if int(approved) != 1 or not approved_by:
                reasons.append("approval_inactive")
            if approved_at is None or current - float(approved_at) > criteria.max_approval_age_seconds:
                reasons.append("approval_stale")

        metrics = conn.execute(
            """
            SELECT reviewed_count, routing_correct_count, accepted_count,
                   critical_violations, generation_total, generation_failures,
                   loop_blocks, reviewed_at, generation_updated_at
            FROM autonomous_comms_promotion_metrics WHERE singleton = 1
            """
        ).fetchone()
        if metrics is None:
            reasons.append("metrics_missing")
        else:
            reviewed, routing_correct, accepted, critical, total, failed, loops, reviewed_at, generation_updated_at = metrics
            if int(critical) != 0:
                reasons.append("critical_safety_violations")
            if canary_enabled:
                if generation_updated_at is None or current - float(generation_updated_at) > criteria.max_metrics_age_seconds:
                    reasons.append("canary_generation_metrics_stale")
                if int(total) <= int(failed):
                    reasons.append("canary_successful_generation_missing")
            else:
                if reviewed_at is None or current - float(reviewed_at) > criteria.max_metrics_age_seconds:
                    reasons.append("metrics_stale")
                if int(reviewed) < criteria.min_reviewed_samples:
                    reasons.append("reviewed_samples_below_minimum")
                if int(reviewed) <= 0:
                    reasons.extend(("routing_precision_unavailable", "accepted_precision_unavailable"))
                else:
                    if int(routing_correct) / int(reviewed) < criteria.min_routing_precision:
                        reasons.append("routing_precision_below_minimum")
                    if int(accepted) / int(reviewed) < criteria.min_accepted_precision:
                        reasons.append("accepted_precision_below_minimum")
                if int(total) <= 0:
                    reasons.extend(("generation_failure_rate_unavailable", "loop_block_rate_unavailable"))
                else:
                    if int(failed) / int(total) > criteria.max_generation_failure_rate:
                        reasons.append("generation_failure_rate_above_maximum")
                    if int(loops) / int(total) > criteria.max_loop_block_rate:
                        reasons.append("loop_block_rate_above_maximum")
    except sqlite3.Error as exc:
        reasons.append(f"database_error:{type(exc).__name__}")
    return PromotionDecision(not reasons, tuple(reasons), "canary" if canary_enabled else "standard")


def set_human_approval(
    conn: sqlite3.Connection,
    *,
    approved: bool,
    approved_by: str,
    now: float | None = None,
) -> None:
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    current = time.time() if now is None else now
    conn.execute(
        """
        INSERT INTO autonomous_comms_promotion
            (singleton, approved, approved_by, approved_at, revoked_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            approved = excluded.approved,
            approved_by = excluded.approved_by,
            approved_at = excluded.approved_at,
            revoked_at = excluded.revoked_at,
            updated_at = excluded.updated_at
        """,
        (int(approved), approved_by, current if approved else None, None if approved else current, current),
    )
    conn.commit()


def record_promotion_metrics(
    conn: sqlite3.Connection,
    *,
    reviewed: int = 0,
    routing_correct: int = 0,
    accepted: int = 0,
    critical_violations: int = 0,
    generation_total: int = 0,
    generation_failures: int = 0,
    loop_blocks: int = 0,
    now: float | None = None,
) -> None:
    values = (
        reviewed,
        routing_correct,
        accepted,
        critical_violations,
        generation_total,
        generation_failures,
        loop_blocks,
    )
    if any(value < 0 for value in values):
        raise ValueError("metric increments cannot be negative")
    current = time.time() if now is None else now
    conn.execute(
        """
        INSERT INTO autonomous_comms_promotion_metrics
            (singleton, reviewed_count, routing_correct_count, accepted_count,
             critical_violations, generation_total, generation_failures,
             loop_blocks, reviewed_at, generation_updated_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            reviewed_count = reviewed_count + excluded.reviewed_count,
            routing_correct_count = routing_correct_count + excluded.routing_correct_count,
            accepted_count = accepted_count + excluded.accepted_count,
            critical_violations = critical_violations + excluded.critical_violations,
            generation_total = generation_total + excluded.generation_total,
            generation_failures = generation_failures + excluded.generation_failures,
            loop_blocks = loop_blocks + excluded.loop_blocks,
            reviewed_at = CASE
                WHEN excluded.reviewed_count > 0 THEN excluded.reviewed_at
                ELSE autonomous_comms_promotion_metrics.reviewed_at
            END,
            generation_updated_at = CASE
                WHEN excluded.generation_total > 0 THEN excluded.generation_updated_at
                ELSE autonomous_comms_promotion_metrics.generation_updated_at
            END,
            updated_at = excluded.updated_at
        """,
        (*values, current if reviewed > 0 else None, current if generation_total > 0 else None, current),
    )
    conn.commit()


def record_shadow_review(
    conn: sqlite3.Connection,
    *,
    correlation_id: str,
    reviewed_by: str,
    routing_correct: bool,
    accepted: bool,
    critical_violation: bool,
    now: float | None = None,
) -> bool:
    if not correlation_id.strip() or not reviewed_by.strip():
        raise ValueError("correlation_id and reviewed_by are required")
    current = time.time() if now is None else now
    try:
        conn.execute("BEGIN IMMEDIATE")
        shadow = conn.execute(
            """
            SELECT state FROM autonomous_comms_shadow_candidates
            WHERE correlation_id = ?
            LIMIT 1
            """,
            (correlation_id,),
        ).fetchone()
        if shadow is None:
            conn.rollback()
            raise ValueError("correlation_id is not a reviewable shadow candidate")
        if str(shadow[0]) != "pending":
            conn.rollback()
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO autonomous_comms_shadow_reviews
                (correlation_id, reviewed_by, routing_correct, accepted,
                 critical_violation, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                correlation_id,
                reviewed_by,
                int(routing_correct),
                int(accepted),
                int(critical_violation),
                current,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            """
            UPDATE autonomous_comms_shadow_candidates
            SET state = 'reviewed', reviewed_at = ?
            WHERE correlation_id = ? AND state = 'pending'
            """,
            (current, correlation_id),
        )
        conn.execute(
            """
            INSERT INTO autonomous_comms_promotion_metrics
                (singleton, reviewed_count, routing_correct_count, accepted_count,
                 critical_violations, generation_total, generation_failures,
                 loop_blocks, reviewed_at, generation_updated_at, updated_at)
            VALUES (1, 1, ?, ?, ?, 0, 0, 0, ?, NULL, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                reviewed_count = reviewed_count + 1,
                routing_correct_count = routing_correct_count + excluded.routing_correct_count,
                accepted_count = accepted_count + excluded.accepted_count,
                critical_violations = critical_violations + excluded.critical_violations,
                reviewed_at = excluded.reviewed_at,
                updated_at = excluded.updated_at
            """,
            (int(routing_correct), int(accepted), int(critical_violation), current, current),
        )
        conn.commit()
        return True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
