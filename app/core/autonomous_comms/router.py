from app.core.autonomous_comms.models import GateInput, MessageType, RouteDecision, RouteVerdict, ThreadState

_TERMINAL_THREAD_STATES: frozenset[ThreadState] = frozenset(
    {
        ThreadState.CLOSED,
        ThreadState.FAILED,
        ThreadState.POISONED,
        ThreadState.EXPIRED,
    }
)


def route(gate_input: GateInput) -> RouteDecision:
    if gate_input.kill_switch_active:
        return RouteDecision(RouteVerdict.REJECT, "kill_switch_active")

    if gate_input.message_type is MessageType.LEGACY:
        return RouteDecision(RouteVerdict.HOLD, "legacy_dispatch_equivalent", pinned_as_dispatch=True)

    if gate_input.message_type is MessageType.DISPATCH:
        return RouteDecision(RouteVerdict.HOLD, "dispatch_hold")

    if gate_input.message_type is not MessageType.DIALOGUE:
        return RouteDecision(RouteVerdict.REJECT, "unknown_message_type")

    facts = gate_input.note_facts

    if facts.thread_state in _TERMINAL_THREAD_STATES:
        return RouteDecision(RouteVerdict.REJECT, "terminal_thread_state")

    if facts.thread_state is ThreadState.UNKNOWN:
        return RouteDecision(RouteVerdict.REJECT, "unknown_thread_state")

    if facts.hop_count >= facts.max_hops:
        return RouteDecision(RouteVerdict.REJECT, "max_hops_exceeded")

    if not gate_input.promotion_active:
        return RouteDecision(RouteVerdict.HOLD, "inactive_promotion_hold")

    if facts.thread_state is ThreadState.OPEN:
        return RouteDecision(RouteVerdict.DIALOGUE, "active_dialogue")

    return RouteDecision(RouteVerdict.REJECT, "unknown_thread_state")
