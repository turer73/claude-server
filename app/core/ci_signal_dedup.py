"""Signal-based de-duplication and lesson-learned storage for CI auto-fix.

Pure/async functions. Sync functions (normalize_error, compute_signature) take
plain strings. Async DB functions take a Database instance from
app.db.database so they can be tested against a tmp_path DB.

See: docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md
"""
from __future__ import annotations

import hashlib
import re

from app.db.database import Database

FIX_DIFF_CAP = 4096

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


async def record_lesson(
    db: Database,
    *,
    run_uuid: str,
    project: str,
    test_name: str,
    error_hash: str,
    signature: str,
    raw_error: str | None,
    attempt_num: int,
    strategy: str,
    context_lessons: str | None,
    fix_diff: str | None,
    outcome: str,
    duration_ms: int | None,
) -> int:
    """Insert one lesson row, returning its id. fix_diff is truncated to FIX_DIFF_CAP."""
    if fix_diff is not None and len(fix_diff) > FIX_DIFF_CAP:
        fix_diff = fix_diff[:FIX_DIFF_CAP]
    cursor = await db.execute(
        """
        INSERT INTO ci_lesson_learned
            (run_uuid, project, test_name, error_hash, signature, raw_error,
             attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_uuid, project, test_name, error_hash, signature, raw_error,
         attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms),
    )
    return cursor.lastrowid


async def get_recent_occurrences(db: Database, signature: str, window: int = 3) -> int:
    """Count 'failed' attempts with this signature across the `window` most recent runs.

    A "run" is a distinct run_uuid. We look at the last `window` run_uuids that
    touched this signature (any outcome), then count how many rows inside those
    runs have outcome='failed'.
    """
    row = await db.fetch_one(
        """
        WITH recent_runs AS (
            SELECT DISTINCT run_uuid, MAX(created_at) AS last_ts
            FROM ci_lesson_learned
            WHERE signature = ?
            GROUP BY run_uuid
            ORDER BY last_ts DESC
            LIMIT ?
        )
        SELECT COUNT(*) AS n
        FROM ci_lesson_learned
        WHERE signature = ?
          AND outcome = 'failed'
          AND run_uuid IN (SELECT run_uuid FROM recent_runs)
        """,
        (signature, window, signature),
    )
    return int((row or {}).get("n") or 0)


async def fetch_lesson_context(
    db: Database, project: str, signature: str, limit: int = 5,
) -> list[dict]:
    """Return past lessons matching (project, signature), newest first."""
    rows = await db.fetch_all(
        """
        SELECT id, run_uuid, attempt_num, strategy, outcome, fix_diff,
               raw_error, created_at
        FROM ci_lesson_learned
        WHERE project = ? AND signature = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (project, signature, limit),
    )
    return rows
