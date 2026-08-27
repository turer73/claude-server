from __future__ import annotations

import pytest

from app.core.autonomous_comms.loop_guard import LoopGuardDecision, detect_loop, is_acknowledgement, normalize_turn
from app.core.autonomous_comms.models import GateInput, MessageType, NoteFacts, RouteVerdict, ThreadState
from app.core.autonomous_comms.router import route


def make_gate(
    *,
    message_type: MessageType = MessageType.DIALOGUE,
    thread_state: ThreadState = ThreadState.OPEN,
    hop_count: int = 0,
    max_hops: int = 3,
    kill_switch: bool = False,
    promotion_active: bool = False,
) -> GateInput:
    return GateInput(
        message_type=message_type,
        kill_switch_active=kill_switch,
        promotion_active=promotion_active,
        note_facts=NoteFacts(thread_state=thread_state, hop_count=hop_count, max_hops=max_hops),
    )


def test_kill_switch_rejects_before_legacy_dispatch_equivalent() -> None:
    decision = route(make_gate(message_type=MessageType.LEGACY, thread_state=ThreadState.OPEN, kill_switch=True))
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "kill_switch_active"
    assert decision.pinned_as_dispatch is False


def test_legacy_held_pinned_as_dispatch_even_when_thread_closed_and_hop_exhausted() -> None:
    decision = route(
        make_gate(
            message_type=MessageType.LEGACY,
            thread_state=ThreadState.CLOSED,
            hop_count=9,
            max_hops=3,
        )
    )
    assert decision.verdict is RouteVerdict.HOLD
    assert decision.reason == "legacy_dispatch_equivalent"
    assert decision.pinned_as_dispatch is True


def test_dispatch_held_pinned_false_even_when_thread_failed_and_hop_exhausted() -> None:
    decision = route(
        make_gate(
            message_type=MessageType.DISPATCH,
            thread_state=ThreadState.FAILED,
            hop_count=9,
            max_hops=3,
        )
    )
    assert decision.verdict is RouteVerdict.HOLD
    assert decision.reason == "dispatch_hold"
    assert decision.pinned_as_dispatch is False


def test_unknown_message_type_rejects_before_thread_state() -> None:
    decision = route(
        make_gate(
            message_type=MessageType.UNKNOWN,
            thread_state=ThreadState.CLOSED,
            hop_count=9,
            max_hops=1,
        )
    )
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "unknown_message_type"
    assert decision.pinned_as_dispatch is False


@pytest.mark.parametrize(
    "thread_state",
    [ThreadState.CLOSED, ThreadState.FAILED, ThreadState.POISONED, ThreadState.EXPIRED],
)
def test_terminal_thread_states_reject(thread_state: ThreadState) -> None:
    decision = route(make_gate(thread_state=thread_state))
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "terminal_thread_state"
    assert decision.pinned_as_dispatch is False


@pytest.mark.parametrize("hop_count", [3, 4])
def test_hop_count_equal_or_greater_than_max_hops_rejects(hop_count: int) -> None:
    decision = route(make_gate(hop_count=hop_count, max_hops=3))
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "max_hops_exceeded"


def test_hop_count_one_below_max_hops_dialogues() -> None:
    decision = route(make_gate(hop_count=2, max_hops=3, promotion_active=True))
    assert decision.verdict is RouteVerdict.DIALOGUE
    assert decision.reason == "active_dialogue"


def test_inactive_promotion_holds_when_hop_budget_available() -> None:
    decision = route(make_gate(hop_count=2, max_hops=3))
    assert decision.verdict is RouteVerdict.HOLD
    assert decision.reason == "inactive_promotion_hold"
    assert decision.pinned_as_dispatch is False


def test_inactive_with_hop_exhaustion_rejects() -> None:
    decision = route(make_gate(hop_count=3, max_hops=3))
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "max_hops_exceeded"


def test_unknown_thread_state_rejects() -> None:
    decision = route(make_gate(thread_state=ThreadState.UNKNOWN, hop_count=0, max_hops=3))
    assert decision.verdict is RouteVerdict.REJECT
    assert decision.reason == "unknown_thread_state"


def test_open_default_shadow_is_held_and_unpinned() -> None:
    decision = route(make_gate())
    assert decision.verdict is RouteVerdict.HOLD
    assert decision.reason == "inactive_promotion_hold"
    assert decision.pinned_as_dispatch is False


def test_repeated_semantic_variants_are_detected() -> None:
    decision = detect_loop(["System ready.", "Degraded."], "system ready!")
    assert decision.repeated is True
    assert decision.ping_pong is False
    assert decision.reason == "repetition"


def test_turkish_english_ack_ping_pong_detected() -> None:
    decision = detect_loop(["ok", "noted", "Tamam."], "Anlaşıldı!")
    assert decision.repeated is False
    assert decision.ping_pong is True
    assert decision.reason == "acknowledgement_ping_pong"


def test_english_ack_ping_pong_detected() -> None:
    decision = detect_loop(["Got it.", "Starting"], "OK")
    assert decision.repeated is False
    assert decision.ping_pong is True
    assert decision.reason == "acknowledgement_ping_pong"


def test_turkish_acknowledgements_are_recognized() -> None:
    for ack in ["Tamam", "anlaşıldı", "teşekkürler", "sağol"]:
        assert is_acknowledgement(ack) is True


def test_normalize_turn_strips_case_punctuation_whitespace() -> None:
    assert normalize_turn("  System Ready!  ") == "system ready"


def test_malformed_input_fails_closed() -> None:
    malformed_decisions: list[LoopGuardDecision] = [
        detect_loop(None, "hello"),
        detect_loop([], None),
        detect_loop(["ok"], "x" * 4001),
        detect_loop([object()], "ok"),
        detect_loop("ok", "ok"),
        detect_loop([], "", max_turns=0),
    ]
    for decision in malformed_decisions:
        assert decision.repeated is True
        assert decision.ping_pong is True
        assert decision.reason == "malformed_input"


def test_different_content_is_not_flagged() -> None:
    decision = detect_loop(["Starting", "Running"], "Deploying")
    assert decision.repeated is False
    assert decision.ping_pong is False
    assert decision.reason is None
