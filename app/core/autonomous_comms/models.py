from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MessageType(StrEnum):
    LEGACY = "legacy"
    DISPATCH = "dispatch"
    DIALOGUE = "dialogue"
    UNKNOWN = "unknown"


class ThreadState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"
    POISONED = "poisoned"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NoteFacts:
    thread_state: ThreadState
    hop_count: int
    max_hops: int

    def __post_init__(self) -> None:
        if self.hop_count < 0:
            raise ValueError("hop_count must be non-negative")
        if self.max_hops <= 0:
            raise ValueError("max_hops must be positive")


@dataclass(frozen=True)
class GateInput:
    message_type: MessageType
    note_facts: NoteFacts
    kill_switch_active: bool = False
    promotion_active: bool = False


class RouteVerdict(StrEnum):
    REJECT = "reject"
    HOLD = "hold"
    DIALOGUE = "dialogue"


@dataclass(frozen=True)
class RouteDecision:
    verdict: RouteVerdict
    reason: str
    pinned_as_dispatch: bool = False
