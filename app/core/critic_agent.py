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
from datetime import UTC, datetime
from typing import Any

from app.core.agent_bus import Event, get_bus

log = logging.getLogger("critic_agent")

_CRITIC_DB_PATH = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")

_FOCUS_BOREDOM_THRESHOLD = 5
_EMOTION_STUCK_THRESHOLD = 4
_CONTENT_REPEAT_THRESHOLD = 0.65


def _get_recent_thoughts(limit: int = 10, skip_id: int = 0) -> list[dict[str, Any]]:
    try:
        con = sqlite3.connect(_CRITIC_DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, timestamp, focus, emotion, content FROM thoughts WHERE id != ? ORDER BY id DESC LIMIT ?",
            (skip_id, limit),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.warning("critic: cannot read thoughts: %s", e)
        return []


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
    recent = _get_recent_thoughts(limit=10, skip_id=skip_id)
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
        self._last_scored_id: int = 0

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
        thought = event.payload.get("thought", {})
        if not thought:
            return
        thought_id = event.payload.get("thought_id", 0)
        if thought_id and thought_id <= self._last_scored_id:
            return
        result = await asyncio.to_thread(_score_thought, thought, skip_id=thought_id)
        self._last_score = result
        self._last_thought_ts = thought.get("timestamp") or datetime.now(UTC).isoformat()
        self._last_scored_id = thought_id
        self._score_history.append(result)
        if len(self._score_history) > 100:
            self._score_history.pop(0)
        self._avg_score = sum(s["score"] for s in self._score_history) / max(len(self._score_history), 1)

        bus = get_bus()
        bus.agent_status("critic", last_score=result["score"], avg_score=round(self._avg_score, 1))

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

    async def _run_loop(self) -> None:
        while self._running:
            try:
                recent = await asyncio.to_thread(_get_recent_thoughts, limit=5, skip_id=self._last_scored_id)
                if not recent:
                    await asyncio.sleep(self._interval)
                    continue
                for t in recent:
                    tid = t.get("id", 0)
                    if tid and tid <= self._last_scored_id:
                        continue
                    await self._on_thought(Event(type="thought:new", source="critic:loop", payload={"thought_id": tid, "thought": t}))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("critic loop error: %s", e)
            await asyncio.sleep(self._interval)
