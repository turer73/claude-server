from __future__ import annotations

import sqlite3

import pytest

from app.core.autonomous_comms.dialogue import (
    DialogueConfig,
    DialogueFailure,
    DialogueProducer,
    DialogueSuccess,
    DialogueTurn,
)
from app.core.autonomous_comms.promotion import (
    PromotionCriteria,
    evaluate_promotion,
    record_promotion_metrics,
    set_human_approval,
)
from app.core.autonomous_comms.schema import ensure_schema


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    ensure_schema(connection)
    return connection


def _good_metrics(connection: sqlite3.Connection, *, now: float = 1_000) -> None:
    record_promotion_metrics(
        connection,
        reviewed=100,
        routing_correct=96,
        accepted=92,
        generation_total=100,
        generation_failures=2,
        loop_blocks=3,
        now=now,
    )


def test_promotion_defaults_shadow_and_flip_alone_is_insufficient(conn: sqlite3.Connection) -> None:
    assert evaluate_promotion(conn, operator_enabled=False, now=1_000).reasons == ("operator_config_off",)
    decision = evaluate_promotion(conn, operator_enabled=True, now=1_000)
    assert decision.active is False
    assert set(decision.reasons) == {"approval_missing", "metrics_missing"}


def test_promotion_active_only_when_all_three_gates_pass(conn: sqlite3.Connection) -> None:
    set_human_approval(conn, approved=True, approved_by="admin", now=1_000)
    _good_metrics(conn)
    assert evaluate_promotion(conn, operator_enabled=True, now=1_000).active is True


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        ({"reviewed": 99}, "reviewed_samples_below_minimum"),
        ({"routing_correct": 94}, "routing_precision_below_minimum"),
        ({"accepted": 89}, "accepted_precision_below_minimum"),
        ({"critical_violations": 1}, "critical_safety_violations"),
        ({"generation_failures": 6}, "generation_failure_rate_above_maximum"),
        ({"loop_blocks": 11}, "loop_block_rate_above_maximum"),
    ],
)
def test_each_promotion_threshold_fails_closed(
    conn: sqlite3.Connection,
    metrics: dict[str, int],
    reason: str,
) -> None:
    set_human_approval(conn, approved=True, approved_by="admin", now=1_000)
    base = {
        "reviewed": 100,
        "routing_correct": 96,
        "accepted": 92,
        "critical_violations": 0,
        "generation_total": 100,
        "generation_failures": 2,
        "loop_blocks": 3,
    }
    base.update(metrics)
    record_promotion_metrics(conn, **base, now=1_000)
    assert reason in evaluate_promotion(conn, operator_enabled=True, now=1_000).reasons


def test_stale_approval_or_metrics_fail_closed(conn: sqlite3.Connection) -> None:
    criteria = PromotionCriteria(max_approval_age_seconds=10, max_metrics_age_seconds=10)
    set_human_approval(conn, approved=True, approved_by="admin", now=100)
    _good_metrics(conn, now=100)
    reasons = evaluate_promotion(conn, operator_enabled=True, criteria=criteria, now=111).reasons
    assert "approval_stale" in reasons
    assert "metrics_stale" in reasons


def test_generation_activity_does_not_refresh_human_review_freshness(conn: sqlite3.Connection) -> None:
    criteria = PromotionCriteria(max_approval_age_seconds=100, max_metrics_age_seconds=10)
    set_human_approval(conn, approved=True, approved_by="admin", now=100)
    _good_metrics(conn, now=100)
    record_promotion_metrics(conn, generation_total=1, now=110)
    reasons = evaluate_promotion(conn, operator_enabled=True, criteria=criteria, now=111).reasons
    assert "metrics_stale" in reasons


def test_database_error_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    decision = evaluate_promotion(connection, operator_enabled=True, now=100)
    assert decision.active is False
    assert decision.reasons[0].startswith("database_error:")


def test_revoke_human_approval_blocks(conn: sqlite3.Connection) -> None:
    set_human_approval(conn, approved=True, approved_by="admin", now=100)
    _good_metrics(conn, now=100)
    set_human_approval(conn, approved=False, approved_by="admin", now=101)
    assert "approval_inactive" in evaluate_promotion(conn, operator_enabled=True, now=101).reasons


def test_dialogue_uses_dedicated_route_bounded_redacted_context() -> None:
    captured: dict[str, object] = {}

    def fake_llm(**kwargs: object) -> str:
        captured.update(kwargs)
        return "Durumu inceleyip kısa bir değerlendirme paylaşabilirim."

    producer = DialogueProducer(
        config=DialogueConfig(max_context_turns=2, max_context_chars=100),
        llm_callable=fake_llm,
    )
    result = producer.produce(
        [
            DialogueTurn("user", "old context"),
            DialogueTurn("assistant", "ok"),
            DialogueTurn("user", "secret ghp_abcdefghijklmnopqrstuvwxyz1234567890"),
        ]
    )
    assert isinstance(result, DialogueSuccess)
    assert captured["task"] == "autonomous_dialogue"
    assert "old context" not in str(captured["prompt"])
    assert "ghp_" not in str(captured["prompt"])
    assert len(str(captured["prompt"])) <= 100


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "empty_reply"),
        ("```sh\nrm -rf /\n```", "action_like_reply"),
        ("Please dispatch this task", "action_like_reply"),
        ("https://user:pass@example.com/a", "credential_url"),
        ("Error: provider down", "provider_error_text"),
        ("ok\x07", "control_characters"),
    ],
)
def test_dialogue_rejects_bad_outputs(reply: str, reason: str) -> None:
    result = DialogueProducer(llm_callable=lambda **_: reply).produce([DialogueTurn("user", "hello")])
    assert isinstance(result, DialogueFailure)
    assert result.reason == reason


def test_dialogue_handles_provider_error_and_empty_context() -> None:
    def fail(**_: object) -> str:
        raise RuntimeError("secret provider detail")

    assert DialogueProducer(llm_callable=fail).produce([DialogueTurn("user", "hello")]) == DialogueFailure("provider_error")
    assert DialogueProducer(llm_callable=lambda **_: "ok").produce([]) == DialogueFailure("empty_context")
