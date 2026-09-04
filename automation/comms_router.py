"""Faz-C otonom-diyalog routing cekirdegi — saf fonksiyon (spawn-yok, test-edilebilir).

Tasarim kaynagi: docs/autonomous-comms-design.md §4 (hop-TTL + policy-gate) ve §11
(route saf fonksiyon + adversarial-CI). Enforcement-ladder G6 deseni:

    route(msg_type, hop_count, thread_state, halt_active, max_hop) -> verdict

Verdict'ler:
- "rejected"  : hicbir otonom-aksiyon uretilmez (spawn atlanir)
- "held"      : cevap uretilir ama asla otomatik GONDERILMEZ — insan-gate'te durur
- "dialogue"  : otonom-gonderim izni (Faz-0 shadow'da HIC donmez; insan FLIP'iyle acilir)

Otorite ucgeni (§3): msg_type server-turetilir (client-iddiasi degil); legacy mesajlar
dispatch-esdegeri sayilir (fail-closed, dialogue'a asla otomatik-terfi etmez).

Invariant (§4): held-sinifi (consequential) hop-count'tan BAGIMSIZ held kalir —
hop-TTL yalniz dialogue-tipi mesajlari kapsar, dispatch'i asla otomatik-akıtmaz.
"""

from __future__ import annotations

DIALOGUE = "dialogue"
DISPATCH = "dispatch"
LEGACY = "legacy"

HELD = "held"
REJECTED = "rejected"

# Faz-0 shadow: otonom-gonderim kapali. Bu sabit insan-karariyla True'ya cevrilmeden
# route() asla "dialogue" donmez (docs §11: terfi Turgut'un gate-FLIP-kontrolunde).
SHADOW_MODE = True

# hop-TTL: dialogue mesajlari icin otomatik-durma derinligi (§4).
DEFAULT_MAX_HOP = 3

# Thread-state degerleri (DLQ/poison ailesi, §6 ile ayni sinif).
_CLOSED_STATES = ("poison", "failed", "closed", "expired")


def route(
    msg_type: str,
    hop_count: int,
    thread_state: str = "open",
    halt_active: bool = False,
    max_hop: int = DEFAULT_MAX_HOP,
) -> str:
    """Otonom-aksiyon karari verir. Yan etkisizdir (spawn/network/DB yok)."""
    if halt_active:
        return REJECTED

    if msg_type == LEGACY:
        msg_type = DISPATCH  # PIN §3: legacy = dispatch esdegeri, fail-closed

    if msg_type == DISPATCH:
        return HELD  # consequential: hop-TTL'den bagimsiz insan-gate

    if msg_type != DIALOGUE:
        return REJECTED  # bilinmeyen tip fail-closed

    if thread_state in _CLOSED_STATES:
        return REJECTED

    if hop_count < 0:
        return REJECTED

    if hop_count >= max_hop:
        return REJECTED  # hop-TTL doldu

    if SHADOW_MODE:
        return HELD  # Faz-0: cevap uretilir ama asla otomatik-gonderilmez

    return DIALOGUE
