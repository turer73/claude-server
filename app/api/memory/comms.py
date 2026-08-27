from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.memory import get_db, router, verify_admin_key
from app.core.autonomous_comms.audit import append_audit
from app.core.autonomous_comms.promotion import (
    PromotionCriteria,
    evaluate_promotion,
    record_shadow_review,
    set_human_approval,
)
from app.core.autonomous_comms.schema import ensure_schema
from app.core.config import read_env_var


class PromotionApproval(BaseModel):
    approved: bool


class ShadowReview(BaseModel):
    correlation_id: str = Field(min_length=16, max_length=128)
    routing_correct: bool
    accepted: bool
    critical_violation: bool = False


def _operator_enabled() -> bool:
    return (read_env_var("AUTONOMOUS_COMMS_ACTIVE") or "").strip().casefold() in {"1", "true", "on", "yes"}


def _canary_enabled() -> bool:
    return (read_env_var("AUTONOMOUS_COMMS_CANARY_ACTIVE") or "").strip().casefold() in {"1", "true", "on", "yes"}


@router.get("/comms/promotion")
async def promotion_status() -> dict[str, object]:
    db = get_db()
    try:
        ensure_schema(db)
        criteria = PromotionCriteria()
        decision = evaluate_promotion(
            db,
            operator_enabled=_operator_enabled(),
            canary_enabled=_canary_enabled(),
            criteria=criteria,
        )
        approval = db.execute(
            """
            SELECT approved, approved_by, approved_at, revoked_at, updated_at
            FROM autonomous_comms_promotion WHERE singleton = 1
            """
        ).fetchone()
        metrics = db.execute(
            """
            SELECT reviewed_count, routing_correct_count, accepted_count,
                   critical_violations, generation_total, generation_failures,
                   loop_blocks, reviewed_at, generation_updated_at, updated_at
            FROM autonomous_comms_promotion_metrics WHERE singleton = 1
            """
        ).fetchone()
        pending = db.execute(
            """
            SELECT COUNT(*) FROM autonomous_comms_shadow_candidates
            WHERE state = 'pending'
            """
        ).fetchone()
        return {
            "mode": "active" if decision.active else "shadow",
            "promotion_profile": decision.profile,
            "operator_enabled": _operator_enabled(),
            "canary_enabled": _canary_enabled(),
            "advisory_only": True,
            "blocking_reasons": list(decision.reasons),
            "approval": None if approval is None else dict(approval),
            "metrics": None if metrics is None else dict(metrics),
            "pending_shadow_reviews": int(pending[0]) if pending else 0,
            "criteria": criteria.__dict__,
        }
    finally:
        db.close()


@router.get("/comms/shadow-candidates", dependencies=[Depends(verify_admin_key)])
async def list_shadow_candidates(limit: int = 50) -> dict[str, object]:
    safe_limit = min(max(limit, 1), 100)
    db = get_db()
    try:
        ensure_schema(db)
        rows = db.execute(
            """
            SELECT correlation_id, thread_id, source_note_id, reply_text, state, created_at
            FROM autonomous_comms_shadow_candidates
            WHERE state = 'pending'
            ORDER BY created_at DESC LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return {"candidates": [dict(row) for row in rows]}
    finally:
        db.close()


@router.put("/comms/promotion/approval", dependencies=[Depends(verify_admin_key)])
async def update_promotion_approval(data: PromotionApproval) -> dict[str, object]:
    db = get_db()
    try:
        ensure_schema(db)
        set_human_approval(db, approved=data.approved, approved_by="human-admin")
        correlation_id = uuid.uuid4().hex
        append_audit(
            db,
            decision="promotion_admin",
            reason="approved" if data.approved else "revoked",
            correlation_id=correlation_id,
            idempotency_key=f"promotion:{correlation_id}",
            metadata={"actor": "human-admin"},
        )
        return {"approved": data.approved, "status": "updated"}
    finally:
        db.close()


@router.post("/comms/promotion/reviews", dependencies=[Depends(verify_admin_key)])
async def review_shadow_candidate(data: ShadowReview) -> dict[str, object]:
    db = get_db()
    try:
        ensure_schema(db)
        try:
            created = record_shadow_review(
                db,
                correlation_id=data.correlation_id,
                reviewed_by="human-admin",
                routing_correct=data.routing_correct,
                accepted=data.accepted,
                critical_violation=data.critical_violation,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not created:
            raise HTTPException(409, "shadow candidate already reviewed")
        append_audit(
            db,
            decision="human_review",
            reason="accepted" if data.accepted else "rejected",
            correlation_id=data.correlation_id,
            idempotency_key=f"review:{data.correlation_id}",
            metadata={
                "routing_correct": data.routing_correct,
                "critical_violation": data.critical_violation,
            },
        )
        return {"status": "recorded", "correlation_id": data.correlation_id}
    finally:
        db.close()
