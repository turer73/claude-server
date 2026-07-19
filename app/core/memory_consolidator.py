"""Hafıza Konsolidasyonu — temporal graph + önem skorlaması.

Tracks state changes over time across the system:
- emotions, focuses, events
- Builds a timeline of "what changed when"
- Scores each memory by importance (recency, frequency, emotional weight, critic score)
- Prunes low-value memories to keep DB lean

Publishes 'memory:consolidated' when significant patterns emerge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from app.core.agent_bus import Event, get_bus

log = logging.getLogger("memory_consolidator")

_MEMORY_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")

_HIGH_SCORE_THRESHOLD = 8
_LOW_SCORE_CLEANUP_AFTER = 72
_MAX_MEMORY_NODES = 2000
_SQLITE_EVENT_RETRY_BASE_DELAY = 0.1
_SQLITE_EVENT_RETRY_MAX_DELAY = 5.0
_RECEIPT_CONSUMER = "memory_consolidator"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_type TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    importance REAL DEFAULT 1.0,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    count INTEGER DEFAULT 1,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_type_key ON memory_nodes(node_type, key);
CREATE TABLE IF NOT EXISTS memory_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    last_seen TEXT DEFAULT (datetime('now')),
    count INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_edges_relation ON memory_edges(relation);
CREATE TABLE IF NOT EXISTS agent_event_receipts (
    consumer TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    processed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (consumer, event_type, event_id)
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    prompt TEXT NOT NULL,
    avg_score REAL,
    applied_at TEXT DEFAULT (datetime('now')),
    metadata TEXT DEFAULT '{}'
);
"""


def _ensure_tables() -> None:
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=10)) as con:
            con.executescript(_SCHEMA)
            # One-time compatibility seed: pre-ledger nodes prove that an older
            # deployment reached at least the corresponding memory mutation.
            con.execute(
                """INSERT OR IGNORE INTO agent_event_receipts (consumer, event_type, event_id)
                   SELECT ?, 'thought', CAST(substr(key, 9) AS INTEGER)
                   FROM memory_nodes
                   WHERE node_type='content_preview' AND key GLOB 'thought:[0-9]*'""",
                (_RECEIPT_CONSUMER,),
            )
            con.execute(
                """INSERT OR IGNORE INTO agent_event_receipts (consumer, event_type, event_id)
                   SELECT ?, 'critic:score', CAST(substr(key, 7) AS INTEGER)
                   FROM memory_nodes
                   WHERE node_type='score' AND key GLOB 'score:[0-9]*'""",
                (_RECEIPT_CONSUMER,),
            )
            con.commit()
    except sqlite3.Error:
        log.exception("memory schema migration failed")
        raise


def _upsert_node_in_connection(
    con: sqlite3.Connection,
    node_type: str,
    key: str,
    value: str,
    importance: float = 1.0,
    meta: str = "{}",
) -> None:
    existing = con.execute("SELECT id, count, importance FROM memory_nodes WHERE node_type=? AND key=?", (node_type, key)).fetchone()
    if existing:
        new_count = existing[1] + 1
        new_imp = min(10.0, existing[2] + importance * 0.1)
        con.execute(
            "UPDATE memory_nodes SET value=?, last_seen=datetime('now'), count=?, importance=? WHERE id=?",
            (value, new_count, new_imp, existing[0]),
        )
    else:
        total = con.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
        if total >= _MAX_MEMORY_NODES:
            con.execute(
                "DELETE FROM memory_nodes WHERE importance < 0.5 AND last_seen < datetime('now', ?)",
                (f"-{_LOW_SCORE_CLEANUP_AFTER} hours",),
            )
        con.execute(
            "INSERT INTO memory_nodes (node_type, key, value, importance, metadata) VALUES (?, ?, ?, ?, ?)",
            (node_type, key, value, importance, meta),
        )


def _upsert_node(node_type: str, key: str, value: str, importance: float = 1.0, meta: str = "{}") -> None:
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
            _upsert_node_in_connection(con, node_type, key, value, importance, meta)
            con.commit()
    except sqlite3.Error as e:
        log.warning("node upsert error: %s", e)


def _upsert_edge_in_connection(con: sqlite3.Connection, source: str, target: str, relation: str) -> None:
    existing = con.execute(
        "SELECT id, count FROM memory_edges WHERE source_key=? AND target_key=? AND relation=?",
        (source, target, relation),
    ).fetchone()
    if existing:
        con.execute("UPDATE memory_edges SET last_seen=datetime('now'), count=count+1 WHERE id=?", (existing[0],))
    else:
        con.execute(
            "INSERT INTO memory_edges (source_key, target_key, relation) VALUES (?, ?, ?)",
            (source, target, relation),
        )


def _upsert_edge(source: str, target: str, relation: str) -> None:
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
            _upsert_edge_in_connection(con, source, target, relation)
            con.commit()
    except sqlite3.Error as e:
        log.warning("edge upsert error: %s", e)


def _claim_event_in_connection(con: sqlite3.Connection, event_type: str, event_id: int) -> bool:
    if event_id <= 0:
        return True
    cursor = con.execute(
        "INSERT OR IGNORE INTO agent_event_receipts (consumer, event_type, event_id) VALUES (?, ?, ?)",
        (_RECEIPT_CONSUMER, event_type, event_id),
    )
    return cursor.rowcount == 1


def _store_thought_memory(
    thought_id: int,
    focus: str,
    emotion: str,
    content: str,
    prev_focus: str | None,
    prev_emotion: str | None,
) -> bool:
    """Atomically persist every graph mutation for one thought.

    Errors deliberately propagate to AgentBus so its per-handler retry can
    repeat the whole transaction without double-counting partial writes.
    """
    with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("BEGIN IMMEDIATE")
        if not _claim_event_in_connection(con, "thought", thought_id):
            con.rollback()
            return False
        _upsert_node_in_connection(con, "focus", f"focus:{focus}", focus, importance=1.0)
        _upsert_node_in_connection(con, "emotion", f"emotion:{emotion}", emotion, importance=0.8)
        _upsert_node_in_connection(
            con,
            "content_preview",
            f"thought:{thought_id}",
            content[:200],
            importance=0.3,
        )
        if prev_focus and prev_focus != focus:
            _upsert_edge_in_connection(con, prev_focus, focus, "transition")
        if prev_emotion and prev_emotion != emotion:
            _upsert_edge_in_connection(con, prev_emotion, emotion, "shift")
        con.commit()
        return True


def _store_critic_memory(
    thought_id: int,
    score: int | float,
    importance: float,
    meta: str,
    thought_focus: str,
    thought_emotion: str,
) -> bool:
    """Atomically persist score and its optional observed edge."""
    with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("BEGIN IMMEDIATE")
        if not _claim_event_in_connection(con, "critic:score", thought_id):
            con.rollback()
            return False
        _upsert_node_in_connection(
            con,
            "score",
            f"score:{thought_id}",
            str(score),
            importance=importance,
            meta=meta,
        )
        if thought_focus and thought_emotion:
            _upsert_edge_in_connection(
                con,
                f"emotion:{thought_emotion}",
                f"focus:{thought_focus}",
                "critic_observed",
            )
        con.commit()
        return True


def _get_last_processed_thought_state() -> tuple[int, str, str] | None:
    """Load the durable timeline head so a new leader need not replay it."""
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
            row = con.execute(
                """SELECT t.id, t.focus, t.emotion
                   FROM thoughts AS t
                   JOIN agent_event_receipts AS r
                     ON r.consumer=? AND r.event_type='thought' AND r.event_id=t.id
                   ORDER BY t.id DESC LIMIT 1""",
                (_RECEIPT_CONSUMER,),
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1] or "idle"), str(row[2] or "calm")
    except sqlite3.Error:
        log.exception("durable memory timeline head could not be loaded")
        raise


def _find_patterns() -> list[dict[str, Any]]:
    """Sik tekrarlayan emotion->focus pattern'lerini bul (critic gozlemlerinden).

    KAYNAK = 'critic_observed' edge'leri (_on_critic_score yazar: emotion:X -> focus:Y).
    Onceki-bug: 'transition' relation'i sorgulaniyordu ama (a) o edge'ler _on_thought'a
    bagli ve _on_thought bus-wiring-gap'i yuzunden hic atesenmiyordu (0 satir), (b) transition
    edge'i FOCUS->FOCUS'tur (bu fonksiyonun dondurdugu emotion->focus semantigiyle uyumsuz).
    Gercek emotion->focus sinyali critic_observed'da (canli + zengin, count'lar >>3). Prefix
    ('emotion:'/'focus:') gosterim icin soyulur.
    """
    patterns = []
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
            rows = con.execute(
                """SELECT source_key, target_key, count
                   FROM memory_edges
                   WHERE relation = 'critic_observed'
                   ORDER BY count DESC LIMIT 10"""
            ).fetchall()
        for r in rows:
            if r[2] >= 3:
                emotion = r[0].removeprefix("emotion:")
                focus = r[1].removeprefix("focus:")
                patterns.append(
                    {
                        "emotion": emotion,
                        "focus": focus,
                        "count": r[2],
                        "pattern": f"{emotion} -> {focus} ({r[2]}x)",
                    }
                )
    except sqlite3.Error as e:
        log.warning("pattern query error: %s", e)
    return patterns


def _get_top_memories(limit: int = 20) -> list[dict[str, Any]]:
    try:
        with closing(sqlite3.connect(_MEMORY_DB, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT node_type, key, value, importance, count, last_seen FROM memory_nodes ORDER BY importance DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


class MemoryConsolidator:
    def __init__(self, interval: int = 60) -> None:
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._db_event_lock = asyncio.Lock()
        self._pattern_cache: list[dict[str, Any]] = []
        self._last_state: dict[str, str] = {}
        self._last_thought_id = 0
        self._last_run: str | None = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "key": "memory-consolidator",
            "name": "Hafıza Konsolidasyonu",
            "role": "Temporal graph · önem skorlaması · pattern tespiti",
            "running": self._running,
            "agent_type": "memory_consolidator",
            "pattern_count": len(self._pattern_cache),
            "last_state": dict(self._last_state),  # snapshot-kopya: cagiran await'inde _on_thought mutasyonuna maruz kalmasin
            "interval_s": self._interval,
            "models": ["kural-tabanlı (önem skorlaması, pattern detection)"],
            # _last_state yalnız focus/emotion tutar — "ts" hiç yazılmadığından last_run ebedi None
            # kalıyordu (dashboard "—" gösteriyordu). Döngü-tick'i ayrı alanda izle.
            "last_run": self._last_run,
            "current_task": f"{len(self._pattern_cache)} pattern izleniyor" if self._pattern_cache else "Hafıza konsolidasyonu bekliyor",
            "stats": {"Pattern": len(self._pattern_cache)},
            "success_rate": None,
            "findings": [],
        }

    def start(self) -> None:
        if self._running:
            return
        # Migration and durable timeline restoration are startup prerequisites.
        # Fail before spawning/subscribing so cohort leadership can fail-stop and
        # another worker/process restart can retry from a clean state.
        _ensure_tables()
        last_state = _get_last_processed_thought_state()
        if last_state is not None:
            self._last_thought_id, focus, emotion = last_state
            self._last_state = {"focus": focus, "emotion": emotion}
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        bus = get_bus()
        bus.register_agent("memory_consolidator", "Temporal memory graph builder")
        bus.subscribe("thought:new", self._on_thought)
        bus.subscribe("thought:deep", self._on_thought)
        bus.subscribe("critic:score", self._on_critic_score)
        log.info("memory consolidator started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        bus = get_bus()
        bus.unsubscribe("thought:new", self._on_thought)
        bus.unsubscribe("thought:deep", self._on_thought)
        bus.unsubscribe("critic:score", self._on_critic_score)
        log.info("memory consolidator stopped")

    async def _on_thought(self, event: Event) -> None:
        await self._persist_event_with_retry(event, self._process_thought)

    async def _process_thought(self, event: Event) -> None:
        thought = event.payload.get("thought", {})
        if not thought:
            return

        focus = thought.get("focus", "idle")
        emotion = thought.get("emotion", "calm")
        content = thought.get("content", "")

        try:
            thought_id = int(event.payload.get("thought_id", 0) or 0)
        except (TypeError, ValueError):
            thought_id = 0
        advances_timeline = thought_id <= 0 or thought_id > self._last_thought_id
        prev_focus = self._last_state.get("focus") if advances_timeline else None
        prev_emotion = self._last_state.get("emotion") if advances_timeline else None

        # One transaction makes an AgentBus retry safe: either every graph mutation
        # commits or none does. Advance the in-memory cursor only after commit, so a
        # failed delivery retries with the same previous state.
        await asyncio.to_thread(
            _store_thought_memory,
            thought_id,
            focus,
            emotion,
            content,
            prev_focus,
            prev_emotion,
        )
        if advances_timeline:
            self._last_state["focus"] = focus
            self._last_state["emotion"] = emotion
            if thought_id > 0:
                self._last_thought_id = thought_id

    async def _on_critic_score(self, event: Event) -> None:
        await self._persist_event_with_retry(event, self._process_critic_score)

    async def _persist_event_with_retry(
        self,
        event: Event,
        processor: Callable[[Event], Awaitable[None]],
    ) -> None:
        """Apply cancelable backpressure until the atomic SQLite write commits."""
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._db_event_lock:
                    await processor(event)
                return
            except sqlite3.Error:
                delay = min(
                    _SQLITE_EVENT_RETRY_BASE_DELAY * (2 ** min(attempt - 1, 8)),
                    _SQLITE_EVENT_RETRY_MAX_DELAY,
                )
                if attempt <= 3 or attempt % 12 == 0:
                    log.warning(
                        "memory event write failed; retrying: type=%s thought_id=%s attempt=%s",
                        event.type,
                        event.payload.get("thought_id", 0),
                        attempt,
                    )
                await asyncio.sleep(delay)

    async def _process_critic_score(self, event: Event) -> None:
        score = event.payload.get("score", 5)
        thought_focus = event.payload.get("thought_focus", "")
        thought_emotion = event.payload.get("thought_emotion", "")
        is_repetitive = event.payload.get("is_repetitive", False)

        imp = score / 10.0
        if is_repetitive:
            imp *= 0.5
        if score >= _HIGH_SCORE_THRESHOLD:
            imp *= 1.5

        meta = f'{{"score":{score},"source":"critic"}}'
        await asyncio.to_thread(
            _store_critic_memory,
            int(event.payload.get("thought_id", 0) or 0),
            score,
            imp,
            meta,
            thought_focus,
            thought_emotion,
        )

    async def _run_loop(self) -> None:
        while self._running:
            try:
                self._last_run = datetime.now(UTC).isoformat()
                async with self._db_event_lock:
                    patterns = await asyncio.to_thread(_find_patterns)
                if patterns != self._pattern_cache and patterns:
                    self._pattern_cache = patterns
                    bus = get_bus()
                    bus.agent_status("memory_consolidator", patterns=patterns[:5])
                    await bus.publish(
                        Event(
                            type="memory:pattern_detected",
                            source="memory_consolidator",
                            payload={"patterns": patterns[:5]},
                        )
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("consolidator loop error: %s", e)
            await asyncio.sleep(self._interval)

    def get_top_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        return _get_top_memories(limit)

    def get_patterns(self) -> list[dict[str, Any]]:
        return self._pattern_cache
