"""Convert durable bus signals into deduplicated work items.

The router is intentionally deterministic.  It is a safety/coordination layer,
not another LLM decision point.  The same event can arrive live and through the
durable dispatcher, so the database uniqueness constraint is authoritative.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from app.core.agent_bus import Event
from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

_ROUTES = {
    "memory:pattern_detected": ("consciousness", "memory_consolidator"),
    "consciousness:concern": ("devops",),
    "consciousness:concerned": ("devops",),
    "critic:score": ("learning_loop",),
    "alert": ("devops",),
    "code_review": ("code_review",),
    "security": ("devops",),
    "incident": ("devops", "memory_consolidator"),
    "learning:threshold_adjusted": ("critic",),
    "spine": ("consciousness",),
    "autonomous_comms:held": ("consciousness",),
    "autonomous_comms:blocked": ("devops", "consciousness"),
    "autonomous_comms:failed": ("devops",),
}

_VOLATILE_KEYS = frozenset({"timestamp", "ts", "time", "id", "created_at", "event_id"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open',
    created_by TEXT,
    assigned_to TEXT,
    project TEXT,
    correlation_id TEXT,
    created_at REAL NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_work_items_state_created
ON work_items(state, created_at DESC);
"""


def _targets_for(event_type: str) -> list[str] | None:
    for prefix, agents in _ROUTES.items():
        if event_type == prefix or event_type.startswith(prefix + ":"):
            return list(agents)
    return None


def _dedup_key(event_type: str, payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in (payload or {}).items() if key not in _VOLATILE_KEYS}
    raw = event_type + "|" + json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _create_work_item_sync(
    event_type: str,
    title: str,
    payload: dict[str, Any],
    created_by: str,
    targets: list[str],
    dedup_key: str,
) -> None:
    conn = get_conn(server_db_path())
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            """INSERT OR IGNORE INTO work_items
               (type, title, priority, state, created_by, assigned_to, project,
                correlation_id, created_at, dedup_key)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event_type,
                title,
                0,
                "open",
                created_by,
                ",".join(targets),
                str(payload.get("project") or "") or None,
                str(payload.get("correlation_id") or "") or None,
                time.time(),
                dedup_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def create_work_item_sync(
    conn: Any,
    *,
    event_type: str,
    title: str,
    payload: dict[str, Any],
    created_by: str,
) -> bool:
    """Persist a deduplicated work item on a caller-owned connection."""
    targets = _targets_for(event_type)
    if not targets:
        return False
    conn.executescript(_SCHEMA)
    cursor = conn.execute(
        """INSERT OR IGNORE INTO work_items
           (type, title, priority, state, created_by, assigned_to, project,
            correlation_id, created_at, dedup_key)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            event_type,
            title[:200],
            0,
            "open",
            created_by,
            ",".join(targets),
            str(payload.get("project") or "") or None,
            str(payload.get("correlation_id") or "") or None,
            time.time(),
            _dedup_key(event_type, payload),
        ),
    )
    conn.commit()
    return bool(cursor.rowcount == 1)


async def route_event(event: Event) -> None:
    """Bus handler that persists actionable signals without duplicate work."""
    targets = _targets_for(event.type)
    if not targets:
        return
    payload = event.payload or {}
    title = (str(payload.get("title") or "") or event.type)[:200]
    key = _dedup_key(event.type, payload)
    try:
        await asyncio.to_thread(_create_work_item_sync, event.type, title, payload, event.source or "", targets, key)
        logger.info("work_item created: %s -> %s", event.type, ",".join(targets))
    except Exception as exc:
        logger.warning("attention route failed (%s): %s", event.type, exc)
