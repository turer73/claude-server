"""Öğrenme Döngüsü — closed-loop improvement from critic feedback.

Connects critic scores back to behavior:
- Tracks avg score over sliding windows (15min, 1h, 24h)
- When score trends downward, adjusts consciousness focus/emotion thresholds
- When patterns repeat, suggests prompt refinements via event
- Version-controls all prompt changes for audit
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from collections import deque
from typing import Any

from app.core.agent_bus import Event, get_bus
from app.core.critic_agent import _CONTENT_REPEAT_THRESHOLD, _FOCUS_BOREDOM_THRESHOLD

log = logging.getLogger("learning_loop")

_MEMORY_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")

_LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    score_before REAL,
    score_after REAL,
    applied TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_DOWNTREND_TRIGGER = 0.5
_MIN_SCORE_BEFORE_LEARN = 4
_MIN_OBSERVATIONS = 10
_LEARN_COOLDOWN = 3600


def _ensure_learning_table() -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.executescript(_LEARNING_SCHEMA)
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("learning schema error: %s", e)


def _record_learning_event(event_type: str, detail: str, score_before: float | None = None, score_after: float | None = None) -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.execute(
            "INSERT INTO learning_events (event_type, detail, score_before, score_after) VALUES (?, ?, ?, ?)",
            (event_type, detail, score_before, score_after),
        )
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("learning event record error: %s", e)


def _load_prompt(component: str) -> str | None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        row = con.execute(
            "SELECT prompt FROM prompt_versions WHERE component=? ORDER BY id DESC LIMIT 1",
            (component,),
        ).fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _save_prompt(component: str, prompt: str, avg_score: float | None = None) -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.execute(
            "INSERT INTO prompt_versions (component, prompt, avg_score) VALUES (?, ?, ?)",
            (component, prompt, avg_score),
        )
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("prompt save error: %s", e)


class LearningLoop:
    def __init__(self, interval: int = 60) -> None:
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._scores: deque[dict[str, Any]] = deque(maxlen=1000)
        self._last_learn_time: float = 0
        self._current_thresholds: dict[str, Any] = {}
        self._learn_count = 0

    @property
    def status(self) -> dict[str, Any]:
        windows = self._get_windows()
        return {
            "key": "learning-loop",
            "name": "Öğrenme Döngüsü",
            "role": "Closed-loop improvement · threshold adjustment",
            "running": self._running,
            "agent_type": "learning_loop",
            "avg_score_15min": windows.get("15min"),
            "avg_score_1h": windows.get("1h"),
            "avg_score_24h": windows.get("24h"),
            "obs_count": len(self._scores),
            "learn_count": self._learn_count,
            "thresholds": self._current_thresholds,
            "interval_s": self._interval,
            "models": ["kural-tabanlı (sliding window, trend detection)"],
            "last_run": self._scores[-1]["ts"] if self._scores else None,
            "current_task": f"{self._learn_count} öğrenme olayı, {len(self._scores)} gözlem" if self._scores else "Veri bekliyor",
            "stats": {"Gözlem": len(self._scores), "Öğrenme": self._learn_count},
            "success_rate": None,
            "findings": [],
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        bus = get_bus()
        bus.register_agent("learning_loop", "Closed-loop improvement engine")
        bus.subscribe("critic:score", self._on_score)
        _ensure_learning_table()
        log.info("learning loop started (interval=%ss)", self._interval)

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
        bus.unsubscribe("critic:score", self._on_score)
        log.info("learning loop stopped")

    def _get_windows(self) -> dict[str, float | None]:
        now = time.time()
        result: dict[str, float | None] = {}
        for name, secs in [("15min", 900), ("1h", 3600), ("24h", 86400)]:
            recent = [s["score"] for s in self._scores if now - s["ts"] < secs]
            result[name] = sum(recent) / len(recent) if recent else None
        return result

    async def _on_score(self, event: Event) -> None:
        self._scores.append(
            {
                "score": event.payload.get("score", 5),
                "ts": time.time(),
                "thought_focus": event.payload.get("thought_focus", ""),
                "thought_emotion": event.payload.get("thought_emotion", ""),
                "is_repetitive": event.payload.get("is_repetitive", False),
                "boredom_issues": event.payload.get("boredom_issues", []),
            }
        )

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._evaluate_and_learn()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("learning loop error: %s", e)
            await asyncio.sleep(self._interval)

    async def _evaluate_and_learn(self) -> None:
        if len(self._scores) < _MIN_OBSERVATIONS:
            return

        windows = self._get_windows()
        now = time.time()
        recent_15min = [s for s in self._scores if now - s["ts"] < 900]

        avg_15min = windows.get("15min")
        avg_1h = windows.get("1h")

        if avg_15min is None or avg_1h is None:
            return

        if avg_15min < _MIN_SCORE_BEFORE_LEARN:
            return

        needs_learn = False
        learn_reason = ""
        new_boredom_count = sum(1 for s in recent_15min if s.get("boredom_issues"))

        if avg_1h and (avg_15min - avg_1h) < -_DOWNTREND_TRIGGER:
            needs_learn = True
            learn_reason = f"downtrend: 15min={avg_15min:.1f} < 1h={avg_1h:.1f}"
        elif new_boredom_count >= 3:
            needs_learn = True
            learn_reason = f"{new_boredom_count}/15min repetitive thoughts"
        elif avg_15min < 5:
            needs_learn = True
            learn_reason = f"low score: 15min={avg_15min:.1f}"

        if needs_learn and (now - self._last_learn_time) > _LEARN_COOLDOWN:
            self._last_learn_time = now
            self._learn_count += 1
            detail = f"learn #{self._learn_count}: {learn_reason}. Prev thresholds: {self._current_thresholds}"

            threshold_adjustments: dict[str, Any] = {}
            if avg_15min < 5:
                threshold_adjustments["boredom_threshold"] = max(3, _FOCUS_BOREDOM_THRESHOLD - 1)
            if new_boredom_count >= 3:
                threshold_adjustments["content_repeat_threshold"] = min(0.8, _CONTENT_REPEAT_THRESHOLD + 0.05)

            self._current_thresholds.update(threshold_adjustments)

            bus = get_bus()
            bus.agent_status(
                "learning_loop",
                learn_count=self._learn_count,
                last_reason=learn_reason,
                thresholds=self._current_thresholds,
            )

            await asyncio.to_thread(
                _record_learning_event,
                "threshold_adjustment",
                detail,
                score_before=avg_15min,
            )

            await bus.publish(
                Event(
                    type="learning:threshold_adjusted",
                    source="learning_loop",
                    payload={
                        "reason": learn_reason,
                        "avg_score_15min": avg_15min,
                        "adjustments": threshold_adjustments,
                        "learn_count": self._learn_count,
                    },
                )
            )
            log.info("learning: %s", detail)

    def get_learning_history(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            con = sqlite3.connect(_MEMORY_DB, timeout=5)
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM learning_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def get_prompt_history(self, component: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        try:
            con = sqlite3.connect(_MEMORY_DB, timeout=5)
            con.row_factory = sqlite3.Row
            if component:
                rows = con.execute(
                    "SELECT * FROM prompt_versions WHERE component=? ORDER BY id DESC LIMIT ?",
                    (component, limit),
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM prompt_versions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []
