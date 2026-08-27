from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping

_FORBIDDEN_KEYS = frozenset({"prompt", "content", "secret", "credential", "password", "api_key", "authorization"})


def _validate_metadata(value: object, *, parent: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or normalized.endswith("_secret"):
                raise ValueError(f"sensitive audit metadata key: {parent}{key}")
            _validate_metadata(child, parent=f"{parent}{key}.")
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_metadata(child, parent=parent)


def append_audit(
    conn: sqlite3.Connection,
    *,
    decision: str,
    reason: str,
    correlation_id: str,
    idempotency_key: str,
    thread_id: int | None = None,
    source_note_id: int | None = None,
    metadata: Mapping[str, object] | None = None,
    now: float | None = None,
) -> int:
    safe_metadata = {} if metadata is None else dict(metadata)
    _validate_metadata(safe_metadata)
    cursor = conn.execute(
        """
        INSERT INTO autonomous_comms_decision_audit
            (created_at, decision, reason, correlation_id, thread_id,
             source_note_id, idempotency_key, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time() if now is None else now,
            decision,
            reason,
            correlation_id,
            thread_id,
            source_note_id,
            idempotency_key,
            json.dumps(safe_metadata, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("audit insert did not return a row id")
    return cursor.lastrowid
