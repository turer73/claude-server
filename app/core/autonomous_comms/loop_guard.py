from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from string import punctuation

MAX_RAW_CHARS = 4000
MAX_NORMALIZED_CHARS = 2000
DEFAULT_MAX_TURNS = 4

_ACKNOWLEDGEMENTS: frozenset[str] = frozenset(
    {
        "ack",
        "acknowledged",
        "agreed",
        "alright",
        "all right",
        "confirm",
        "confirmed",
        "cool",
        "copy",
        "copy that",
        "done",
        "fine",
        "good",
        "got it",
        "gotcha",
        "great",
        "k",
        "kk",
        "no problem",
        "noted",
        "ok",
        "okay",
        "perfect",
        "received",
        "right",
        "roger",
        "sure",
        "thx",
        "thank you",
        "thanks",
        "thankyou",
        "understood",
        "wilco",
        "yeah",
        "yep",
        "yes",
        "anlasildi",
        "anlaşıldı",
        "aynen",
        "eyvallah",
        "görüşürüz",
        "gorusuruz",
        "iyi",
        "kabul",
        "not edildi",
        "notedildi",
        "oldu",
        "olur",
        "peki",
        "pekala",
        "pekiyi",
        "sağol",
        "sağ ol",
        "sagol",
        "sag ol",
        "tamam",
        "tamamdir",
        "tesekkur ederim",
        "tesekkurler",
        "teşekkür ederim",
        "teşekkürler",
    }
)

_PUNCTUATION = frozenset(punctuation + "…“”‘’«»")
_TRANSLATION_TABLE = str.maketrans("", "", "".join(_PUNCTUATION))


@dataclass(frozen=True)
class LoopGuardDecision:
    repeated: bool
    ping_pong: bool
    reason: str | None = None


def normalize_turn(
    turn: str,
    *,
    max_raw_chars: int = MAX_RAW_CHARS,
    max_normalized_chars: int = MAX_NORMALIZED_CHARS,
) -> str:
    if not isinstance(turn, str):
        raise ValueError("turn must be a string")
    if len(turn) > max_raw_chars:
        raise ValueError("turn exceeds maximum raw length")

    normalized = unicodedata.normalize("NFKC", turn).casefold()
    normalized = normalized.translate(_TRANSLATION_TABLE)
    normalized = " ".join(normalized.split())

    if len(normalized) > max_normalized_chars:
        raise ValueError("turn exceeds maximum normalized length")
    return normalized


def _hash_turn(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_acknowledgement(turn: str) -> bool:
    try:
        return normalize_turn(turn) in _ACKNOWLEDGEMENTS
    except ValueError:
        return False


def detect_loop(
    recent_turns: Sequence[str],
    current_turn: str,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> LoopGuardDecision:
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
        return LoopGuardDecision(True, True, "malformed_input")
    if isinstance(recent_turns, str):
        return LoopGuardDecision(True, True, "malformed_input")

    try:
        recent_list = list(recent_turns)
    except TypeError:
        return LoopGuardDecision(True, True, "malformed_input")

    if len(recent_list) > max_turns:
        recent_list = recent_list[-max_turns:]

    try:
        current_norm = normalize_turn(current_turn)
        recent_norm = [normalize_turn(turn) for turn in recent_list]
    except (TypeError, ValueError):
        return LoopGuardDecision(True, True, "malformed_input")

    if not current_norm or any(not turn for turn in recent_norm):
        return LoopGuardDecision(True, True, "malformed_input")

    current_hash = _hash_turn(current_norm)
    recent_hashes = {_hash_turn(turn) for turn in recent_norm}
    repeated = current_hash in recent_hashes

    current_is_ack = current_norm in _ACKNOWLEDGEMENTS
    recent_ack_flags = [turn in _ACKNOWLEDGEMENTS for turn in recent_norm]
    ping_pong = current_is_ack and any(recent_ack_flags)

    if repeated and ping_pong:
        reason = "repetition_and_acknowledgement_ping_pong"
    elif repeated:
        reason = "repetition"
    elif ping_pong:
        reason = "acknowledgement_ping_pong"
    else:
        reason = None

    return LoopGuardDecision(repeated=repeated, ping_pong=ping_pong, reason=reason)
