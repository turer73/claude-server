"""Signal-based de-duplication and lesson-learned storage for CI auto-fix.

Pure/async functions. Sync functions (normalize_error, compute_signature) take
plain strings. Async DB functions take a Database instance from
app.db.database so they can be tested against a tmp_path DB.

See: docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md
"""
from __future__ import annotations

import hashlib
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


def compute_signature(project: str, test_name: str, raw_error: str) -> tuple[str, str]:
    """Return (error_hash, full_signature).

    error_hash = first 12 hex chars of sha1(normalize_error(raw_error)).
    full_signature = f"{project}::{test_name}::{error_hash}".
    """
    normalized = normalize_error(raw_error)
    error_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return error_hash, f"{project}::{test_name}::{error_hash}"
