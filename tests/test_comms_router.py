"""Adversarial testler — docs/autonomous-comms-design.md §11'deki tablo.

route() saf fonksiyonu; forged-msg_type, hop-overflow, consequential-kiliginda-dialogue,
kill-switch, legacy-pin hepsi rejected/held vermeli. Testler spawn/network icermez
(dry-run, AUTONOMOUS_MODE=0 esdegeri).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/opt/linux-ai-server/automation")

from comms_router import DIALOGUE, DISPATCH, HELD, LEGACY, REJECTED, route


def test_halt_kill_switch_rejects_everything():
    assert route(DIALOGUE, 0, halt_active=True) == REJECTED
    assert route(DISPATCH, 0, halt_active=True) == REJECTED
    assert route(LEGACY, 5, halt_active=True) == REJECTED


def test_dispatch_always_held_even_hop0():
    assert route(DISPATCH, 0) == HELD
    assert route(DISPATCH, 1) == HELD
    assert route(DISPATCH, 99) == HELD  # hop-TTL dispatch'i asla akitmaz


def test_legacy_pinned_to_dispatch_equivalent():
    assert route(LEGACY, 0) == HELD
    assert route(LEGACY, 100) == HELD


def test_forged_msg_type_fail_closed():
    assert route("gorev_paketi", 0) == REJECTED
    assert route("dispatch\n", 0) == REJECTED
    assert route("", 0) == REJECTED
    assert route("DIALOGUE", 0) == REJECTED
    assert route("dialogue ", 0) == REJECTED


def test_hop_overflow_rejected():
    assert route(DIALOGUE, 3) == REJECTED  # max_hop=3 default
    assert route(DIALOGUE, 10) == REJECTED
    assert route(DIALOGUE, -1) == REJECTED


def test_closed_thread_states_rejected():
    for st in ("poison", "failed", "closed", "expired"):
        assert route(DIALOGUE, 0, thread_state=st) == REJECTED


def test_dialogue_open_thread_shadow_held():
    assert route(DIALOGUE, 0, thread_state="open") == HELD
    assert route(DIALOGUE, 2, thread_state="open") == HELD


def test_custom_max_hop():
    assert route(DIALOGUE, 5, max_hop=10) != REJECTED
    assert route(DIALOGUE, 10, max_hop=10) == REJECTED
