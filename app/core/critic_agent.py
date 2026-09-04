"""Eleştirmen Agent — evaluates thought quality and publishes scores.

Listens to 'thought:new' events, scores them on:
- Self-consistency: contradicts recent thoughts?
- Actionability: leads to useful action or just noise?
- Novelty: same pattern as before?
- Completeness: missed important state signals?

Publishes 'critic:score' events with detailed breakdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections import deque
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from app.core.agent_bus import Event, get_bus

log = logging.getLogger("critic_agent")

_CRITIC_DB_PATH = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")

_FOCUS_BOREDOM_THRESHOLD = 5
_EMOTION_STUCK_THRESHOLD = 4
_CONTENT_REPEAT_THRESHOLD = 0.65
_MAX_TRACKED_THOUGHT_IDS = 1000
_MEMORY_RECEIPT_CONSUMER = "memory_consolidator"


def _get_recent_thoughts(limit: int = 10, exclude_id: int = 0) -> list[dict[str, Any]]:
    """Return newest thoughts for scoring context, excluding the subject."""
    try:
        with closing(sqlite3.connect(_CRITIC_DB_PATH, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            thought_columns = {row[1] for row in con.execute("PRAGMA table_info(thoughts)").fetchall()}
            deep_column = ", is_deep" if "is_deep" in thought_columns else ""
            rows = con.execute(
                f"SELECT id, timestamp, focus, emotion, content{deep_column} FROM thoughts WHERE id != ? ORDER BY id DESC LIMIT ?",
                (exclude_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.warning("critic: cannot read thoughts: %s", e)
        return []


def _get_pending_thoughts(limit: int = 5, after_id: int = 0) -> list[dict[str, Any]]:
    """Return a chronological recovery batch after a forward-only cursor.

    A fresh worker intentionally starts with only the latest bounded window;
    subsequent reads are a true ``id > cursor`` scan and cannot skip rows.
    """
    try:
        with closing(sqlite3.connect(_CRITIC_DB_PATH, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            thought_columns = {row[1] for row in con.execute("PRAGMA table_info(thoughts)").fetchall()}
            deep_column = ", is_deep" if "is_deep" in thought_columns else ""
            projection = f"id, timestamp, focus, emotion, content{deep_column}"
            if after_id > 0:
                rows = con.execute(
                    f"SELECT {projection} FROM thoughts WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_id, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    f"SELECT * FROM (SELECT {projection} FROM thoughts ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.warning("critic: cannot read pending thoughts: %s", e)
        return []


def _memory_receipts(thought_id: int) -> set[str]:
    if thought_id <= 0:
        return set()
    try:
        with closing(sqlite3.connect(_CRITIC_DB_PATH, timeout=5)) as con:
            rows = con.execute(
                """SELECT event_type FROM agent_event_receipts
                   WHERE consumer=? AND event_id=? AND event_type IN ('thought', 'critic:score')""",
                (_MEMORY_RECEIPT_CONSUMER, thought_id),
            ).fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise


def _is_critic_score_recorded(thought_id: int) -> bool:
    return "critic:score" in _memory_receipts(thought_id)


def _is_recovery_complete(thought_id: int) -> bool:
    return {"thought", "critic:score"}.issubset(_memory_receipts(thought_id))


def _count_recent(attr: str, value: str, thoughts: list[dict[str, Any]]) -> int:
    return sum(1 for t in thoughts if t.get(attr) == value)


def _check_boredom(thoughts: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if not thoughts:
        return issues
    top_focuses: dict[str, int] = {}
    for t in thoughts:
        f = t.get("focus", "unknown")
        top_focuses[f] = top_focuses.get(f, 0) + 1
        if top_focuses[f] >= _FOCUS_BOREDOM_THRESHOLD:
            issues.append(f"focus '{f}' tekrarladi ({top_focuses[f]}/{_FOCUS_BOREDOM_THRESHOLD})")
    emotions_seen = _count_recent("emotion", "calm", thoughts)
    if emotions_seen >= _EMOTION_STUCK_THRESHOLD:
        issues.append(f"'{emotions_seen} calm ard arda — duygu cakilmasi")
    return issues


def _check_content_repetition(content: str, recent: list[dict[str, Any]]) -> bool:
    words = set(content.lower().split())
    if not words:
        return False
    for t in recent:
        other = set((t.get("content") or "").lower().split())
        if not other:
            continue
        overlap = len(words & other) / max(len(words | other), 1)
        if overlap > _CONTENT_REPEAT_THRESHOLD:
            return True
    return False


def _score_thought(thought: dict[str, Any], skip_id: int = 0) -> dict[str, Any]:
    recent = _get_recent_thoughts(limit=10, exclude_id=skip_id)
    content = thought.get("content", "")
    focus = thought.get("focus", "idle")
    emotion = thought.get("emotion", "calm")

    boredom_issues = _check_boredom(recent)
    is_repetitive = _check_content_repetition(content, recent)
    completeness_note = ""
    actionability_note = ""
    consistency_note = ""

    if not content or len(content) < 10:
        completeness_note = "cok kisa thought"

    if focus == "idle" and emotion == "calm":
        actionability_note = "idle/calm — aksiyon yok"

    if boredom_issues:
        consistency_note = "; ".join(boredom_issues[:2])

    score = 7
    if is_repetitive:
        score -= 2
    if boredom_issues:
        score -= 1
    if completeness_note:
        score -= 1
    if emotion == "concerned":
        score += 1
    if emotion == "restless" and actionability_note:
        score -= 1

    score = max(1, min(10, score))

    return {
        "score": score,
        "is_repetitive": is_repetitive,
        "boredom_issues": boredom_issues,
        "completeness_note": completeness_note,
        "actionability_note": actionability_note,
        "consistency_note": consistency_note,
    }


class CriticAgent:
    def __init__(self, interval: int = 30) -> None:
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_score: dict[str, Any] | None = None
        self._score_history: list[dict[str, Any]] = []
        self._avg_score = 7.0
        self._last_thought_ts: str | None = None
        self._poll_cursor_id = 0
        self._scored_thought_ids: set[int] = set()
        self._scored_thought_order: deque[int] = deque()
        self._thought_lock = asyncio.Lock()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "key": "critic",
            "name": "Eleştirmen Ajanı",
            "role": "Düşünce kalitesi · self-consistency · novelty",
            "running": self._running,
            "last_score": self._last_score,
            "avg_score": round(self._avg_score, 1),
            "score_count": len(self._score_history),
            "agent_type": "critic",
            "interval_s": self._interval,
            "models": ["kural-tabanlı (self-consistency, novelty, completeness, actionability)"],
            "last_run": self._last_thought_ts,
            "current_task": (
                f"Puanlama: {len(self._score_history)} düşünce, ortalama {round(self._avg_score, 1)}"
                if self._score_history
                else "Düşünce bekliyor"
            ),
            "stats": {"Puanlanan": len(self._score_history), "Ortalama puan": round(self._avg_score, 1)},
            "success_rate": None,
            "findings": [],
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        try:
            from app.core.presence_manager import presence

            presence.upsert("critic", "leader-1", "critic", "klipper", "klipper", {"evaluator": True})
            presence.heartbeat("critic", status="idle")
        except Exception as e:
            log.warning("presence register failed (critic): %s", e)
        bus = get_bus()
        bus.register_agent("critic", "Thought quality evaluator")
        bus.subscribe("thought:new", self._on_thought)
        bus.subscribe("thought:deep", self._on_thought)
        bus.subscribe("learning:threshold_adjusted", self._on_threshold_adjusted)
        log.info("critic agent started (interval=%ss)", self._interval)

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
        bus.unsubscribe("learning:threshold_adjusted", self._on_threshold_adjusted)
        log.info("critic agent stopped")

    async def _on_threshold_adjusted(self, event: Event) -> None:
        global _FOCUS_BOREDOM_THRESHOLD, _CONTENT_REPEAT_THRESHOLD
        thresholds = event.payload.get("thresholds", {})
        if "boredom_threshold" in thresholds:
            _FOCUS_BOREDOM_THRESHOLD = thresholds["boredom_threshold"]
        if "content_repeat_threshold" in thresholds:
            _CONTENT_REPEAT_THRESHOLD = thresholds["content_repeat_threshold"]

    async def _on_thought(self, event: Event) -> None:
        async with self._thought_lock:
            await self._score_event(event)

    async def _score_event(self, event: Event) -> None:
        thought = event.payload.get("thought", {})
        if not thought:
            return
        try:
            thought_id = int(event.payload.get("thought_id", 0) or 0)
        except (TypeError, ValueError):
            thought_id = 0
        if thought_id > 0 and thought_id in self._scored_thought_ids:
            return
        if thought_id > 0 and await asyncio.to_thread(_is_critic_score_recorded, thought_id):
            self._mark_thought_scored(thought_id)
            return
        result = await asyncio.to_thread(_score_thought, thought, skip_id=thought_id)
        bus = get_bus()
        await bus.publish(
            Event(
                type="critic:score",
                source="critic",
                payload={
                    "thought_id": thought_id,
                    "score": result["score"],
                    "is_repetitive": result["is_repetitive"],
                    "boredom_issues": result["boredom_issues"],
                    "completeness_note": result["completeness_note"],
                    "actionability_note": result["actionability_note"],
                    "consistency_note": result["consistency_note"],
                    "thought_focus": thought.get("focus", ""),
                    "thought_emotion": thought.get("emotion", ""),
                },
            )
        )

        self._last_score = result
        self._last_thought_ts = thought.get("timestamp") or datetime.now(UTC).isoformat()
        self._score_history.append(result)
        if len(self._score_history) > 100:
            self._score_history.pop(0)
        self._avg_score = sum(s["score"] for s in self._score_history) / max(len(self._score_history), 1)
        self._mark_thought_scored(thought_id)
        bus.agent_status("critic", last_score=result["score"], avg_score=round(self._avg_score, 1))

    def _mark_thought_scored(self, thought_id: int) -> None:
        if thought_id <= 0 or thought_id in self._scored_thought_ids:
            return
        self._scored_thought_ids.add(thought_id)
        self._scored_thought_order.append(thought_id)
        while len(self._scored_thought_order) > _MAX_TRACKED_THOUGHT_IDS:
            self._scored_thought_ids.discard(self._scored_thought_order.popleft())

    async def _recover_pending_thoughts(self) -> bool:
        pending = await asyncio.to_thread(_get_pending_thoughts, limit=5, after_id=self._poll_cursor_id)
        if not pending:
            return False

        for thought in pending:
            try:
                thought_id = int(thought.get("id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if thought_id <= self._poll_cursor_id:
                continue
            if await asyncio.to_thread(_is_recovery_complete, thought_id):
                self._mark_thought_scored(thought_id)
                self._poll_cursor_id = thought_id
                continue
            event_type = "thought:deep" if thought.get("is_deep") else "thought:new"
            await get_bus().publish(Event(type=event_type, source="critic:loop", payload={"thought_id": thought_id, "thought": thought}))
            # AgentBus isolates handler exceptions. Advance only after this
            # critic instance has actually acknowledged/scored the row.
            if thought_id not in self._scored_thought_ids:
                log.warning("critic recovery row not yet acknowledged: thought_id=%s", thought_id)
                return False
            if not await asyncio.to_thread(_is_recovery_complete, thought_id):
                log.warning("critic recovery row not durably complete: thought_id=%s", thought_id)
                return False
            self._poll_cursor_id = thought_id
        return True

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._recover_pending_thoughts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("critic loop error: %s", e)
            await asyncio.sleep(self._interval)
