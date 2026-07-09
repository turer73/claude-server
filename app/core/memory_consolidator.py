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
import sqlite3
from typing import Any

from app.core.agent_bus import Event, get_bus

log = logging.getLogger("memory_consolidator")

_MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"

_HIGH_SCORE_THRESHOLD = 8
_LOW_SCORE_CLEANUP_AFTER = 72
_MAX_MEMORY_NODES = 2000

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
        con = sqlite3.connect(_MEMORY_DB, timeout=10)
        con.executescript(_SCHEMA)
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("memory_node schema ensure failed: %s", e)


def _upsert_node(node_type: str, key: str, value: str, importance: float = 1.0, meta: str = "{}") -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        existing = con.execute(
            "SELECT id, count, importance FROM memory_nodes WHERE node_type=? AND key=?", (node_type, key)
        ).fetchone()
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
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("node upsert error: %s", e)


def _upsert_edge(source: str, target: str, relation: str) -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        existing = con.execute(
            "SELECT id, count FROM memory_edges WHERE source_key=? AND target_key=? AND relation=?",
            (source, target, relation),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE memory_edges SET last_seen=datetime('now'), count=count+1 WHERE id=?", (existing[0],)
            )
        else:
            con.execute(
                "INSERT INTO memory_edges (source_key, target_key, relation) VALUES (?, ?, ?)",
                (source, target, relation),
            )
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("edge upsert error: %s", e)


def _find_patterns() -> list[dict[str, Any]]:
    """Find recurring patterns: emotion→focus transitions that repeat often."""
    patterns = []
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        rows = con.execute(
            """SELECT e.source_key as emotion, e.target_key as focus,
                      e.count as transition_count, e.relation
               FROM memory_edges e
               WHERE e.relation = 'transition'
               ORDER BY e.count DESC LIMIT 10"""
        ).fetchall()
        con.close()
        for r in rows:
            if r[2] >= 3:
                patterns.append({
                    "emotion": r[0],
                    "focus": r[1],
                    "count": r[2],
                    "pattern": f"{r[0]} -> {r[1]} ({r[2]}x)",
                })
    except sqlite3.Error as e:
        log.warning("pattern query error: %s", e)
    return patterns


def _get_top_memories(limit: int = 20) -> list[dict[str, Any]]:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT node_type, key, value, importance, count, last_seen FROM memory_nodes ORDER BY importance DESC LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


class MemoryConsolidator:
    def __init__(self, interval: int = 60) -> None:
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._pattern_cache: list[dict[str, Any]] = []
        self._last_state: dict[str, str] = {}

    @property
    def status(self) -> dict[str, Any]:
        return {
            "key": "memory-consolidator",
            "name": "Hafıza Konsolidasyonu",
            "role": "Temporal graph · önem skorlaması · pattern tespiti",
            "running": self._running,
            "agent_type": "memory_consolidator",
            "pattern_count": len(self._pattern_cache),
            "last_state": self._last_state,
            "interval_s": self._interval,
            "models": ["kural-tabanlı (önem skorlaması, pattern detection)"],
            "last_run": self._last_state.get("ts"),
            "current_task": f"{len(self._pattern_cache)} pattern izleniyor" if self._pattern_cache else "Hafıza konsolidasyonu bekliyor",
            "stats": {"Pattern": len(self._pattern_cache)},
            "success_rate": None,
            "findings": [],
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        bus = get_bus()
        bus.register_agent("memory_consolidator", "Temporal memory graph builder")
        bus.subscribe("thought:new", self._on_thought)
        bus.subscribe("critic:score", self._on_critic_score)
        _ensure_tables()
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
            log.info("memory consolidator stopped")

    async def _on_thought(self, event: Event) -> None:
        thought = event.payload.get("thought", {})
        if not thought:
            return

        focus = thought.get("focus", "idle")
        emotion = thought.get("emotion", "calm")
        content = thought.get("content", "")

        await asyncio.to_thread(_upsert_node, "focus", f"focus:{focus}", focus, importance=1.0)
        await asyncio.to_thread(_upsert_node, "emotion", f"emotion:{emotion}", emotion, importance=0.8)
        thought_key = f"thought:{event.payload.get('thought_id', 0)}"
        await asyncio.to_thread(_upsert_node, "content_preview", thought_key, content[:200], importance=0.3)

        prev_focus = self._last_state.get("focus")
        prev_emotion = self._last_state.get("emotion")
        if prev_focus and prev_focus != focus:
            await asyncio.to_thread(_upsert_edge, prev_focus, focus, "transition")
        if prev_emotion and prev_emotion != emotion:
            await asyncio.to_thread(_upsert_edge, prev_emotion, emotion, "shift")

        self._last_state["focus"] = focus
        self._last_state["emotion"] = emotion

    async def _on_critic_score(self, event: Event) -> None:
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
            _upsert_node, "score", f"score:{event.payload.get('thought_id', 0)}", str(score),
            importance=imp, meta=meta,
        )

        if thought_focus and thought_emotion:
            await asyncio.to_thread(_upsert_edge, f"emotion:{thought_emotion}", f"focus:{thought_focus}", "critic_observed")

    async def _run_loop(self) -> None:
        while self._running:
            try:
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
