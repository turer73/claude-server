"""Signal-based de-duplication and lesson-learned storage for CI auto-fix.

Pure/async functions. Sync functions (normalize_error, compute_signature) take
plain strings. Async DB functions take a Database instance from
app.db.database so they can be tested against a tmp_path DB.

See: docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md
"""
from __future__ import annotations

import hashlib  # noqa: F401  # used by compute_signature in a follow-up task
import re

NOISE_PATTERNS: list[tuple[str, str]] = [
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "<TS>"),
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<TS>"),
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>"),
    (r"0x[0-9a-f]+", "<HEX>"),
    (r"/tmp/[^\s)'\"\]\}>,;]+", "<TMPPATH>"),
    (r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)[^\s)'\"\]\}>,;]+", "<USERPATH>"),
    (r":\d{4,5}\b", ":<PORT>"),
    (r"\b\d{10,}\b", "<BIGINT>"),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), sub) for pat, sub in NOISE_PATTERNS]


def normalize_error(raw: str) -> str:
    """Replace noisy substrings with stable placeholders.

    Idempotent: running it twice yields the same string as once.
    """
    out = raw
    for rx, repl in _COMPILED:
        out = rx.sub(repl, out)
    return out
