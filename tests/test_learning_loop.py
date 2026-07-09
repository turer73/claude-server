"""Tests for LearningLoop — closed-loop improvement from critic feedback."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from app.core.learning_loop import (
    _ensure_learning_table,
    _load_prompt,
    _record_learning_event,
    _save_prompt,
)


@pytest.fixture
def learning_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", db_path)
    monkeypatch.setattr("app.core.memory_consolidator._MEMORY_DB", db_path)
    _ensure_learning_table()
    from app.core.memory_consolidator import _ensure_tables as _ensure_memory_tables

    _ensure_memory_tables()
    yield db_path
    try:
        os.unlink(db_path)
    except PermissionError:
        pass


class TestDbOperations:
    def test_ensure_table_creates_schema(self, learning_db):
        con = sqlite3.connect(learning_db)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        con.close()
        assert "learning_events" in tables

    def test_record_learning_event(self, learning_db):
        _record_learning_event("threshold_adjustment", "test event", score_before=5.0, score_after=6.0)
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT event_type, detail, score_before, score_after FROM learning_events").fetchone()
        con.close()
        assert row[0] == "threshold_adjustment"
        assert row[1] == "test event"
        assert row[2] == 5.0
        assert row[3] == 6.0

    def test_save_and_load_prompt(self, learning_db):
        _save_prompt("consciousness", "You are a helpful assistant.", avg_score=7.5)
        loaded = _load_prompt("consciousness")
        assert loaded == "You are a helpful assistant."

    def test_load_prompt_not_found(self, learning_db):
        assert _load_prompt("nonexistent") is None

    def test_save_multiple_prompt_versions(self, learning_db):
        _save_prompt("consciousness", "version 1", avg_score=6.0)
        _save_prompt("consciousness", "version 2", avg_score=8.0)
        loaded = _load_prompt("consciousness")
        assert loaded == "version 2"


class TestLearningWindow:
    def test_get_windows(self):
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        now = time.time()
        loop._scores.extend(
            [
                {"score": 7, "ts": now - 100},
                {"score": 8, "ts": now - 200},
                {"score": 6, "ts": now - 300},
                {"score": 5, "ts": now - 2000},
            ]
        )
        windows = loop._get_windows()
        assert "15min" in windows
        assert windows["15min"] is not None
        assert windows["1h"] is not None
        assert 6.0 <= windows["15min"] <= 8.0

    def test_get_windows_empty(self):
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        windows = loop._get_windows()
        assert windows["15min"] is None
        assert windows["1h"] is None
        assert windows["24h"] is None

    def test_on_score_appends_correctly(self):
        from app.core.agent_bus import Event
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        event = Event(
            type="critic:score",
            source="critic",
            payload={
                "score": 8,
                "thought_focus": "debug",
                "thought_emotion": "focused",
                "is_repetitive": False,
                "boredom_issues": [],
            },
        )

        import asyncio

        asyncio.run(loop._on_score(event))

        assert len(loop._scores) == 1
        assert loop._scores[0]["score"] == 8
        assert loop._scores[0]["thought_focus"] == "debug"


class TestEvaluateAndLearn:
    def test_not_enough_observations(self):
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        loop._scores.append({"score": 7, "ts": time.time()})

        import asyncio

        asyncio.run(loop._evaluate_and_learn())
        assert loop._learn_count == 0

    def test_downtrend_triggers_learn(self, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        now = time.time()
        loop._MIN_OBSERVATIONS = 3
        loop._DOWNTREND_TRIGGER = 0.5

        scores = [
            {"score": 7, "ts": now - 4000, "boredom_issues": []},
            {"score": 7, "ts": now - 3800, "boredom_issues": []},
            {"score": 7, "ts": now - 3600, "boredom_issues": []},
            {"score": 4, "ts": now - 100, "boredom_issues": []},
            {"score": 4, "ts": now - 80, "boredom_issues": []},
            {"score": 4, "ts": now - 60, "boredom_issues": []},
        ]
        loop._scores.extend(scores)

        import asyncio

        asyncio.run(loop._evaluate_and_learn())

    def test_boredom_trigger(self, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        now = time.time()
        loop._MIN_OBSERVATIONS = 3

        scores = [
            {"score": 7, "ts": now - 4000, "boredom_issues": []},
            {"score": 7, "ts": now - 3800, "boredom_issues": []},
            {"score": 7, "ts": now - 3600, "boredom_issues": []},
            {"score": 5, "ts": now - 200, "boredom_issues": ["focus 'debug' tekrarladi"]},
            {"score": 5, "ts": now - 150, "boredom_issues": ["focus 'debug' tekrarladi"]},
            {"score": 5, "ts": now - 100, "boredom_issues": ["focus 'debug' tekrarladi"]},
        ]
        loop._scores.extend(scores)

    def test_get_learning_history_empty(self, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        assert loop.get_learning_history() == []

    def test_get_prompt_history_empty(self, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        assert loop.get_prompt_history() == []

    def test_error_handling_bad_db_path(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        from app.core.learning_loop import _load_prompt, _record_learning_event, _save_prompt

        _record_learning_event("test", "detail")
        assert _load_prompt("any") is None
        _save_prompt("c", "p")

    def test_ensure_table_no_crash(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        _ensure_learning_table()

    def test_get_learning_history_bad_db(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        assert loop.get_learning_history() == []

    def test_get_prompt_history_bad_db(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        assert loop.get_prompt_history() == []


@pytest.fixture
def learning_loop(learning_db, monkeypatch):
    monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
    from app.core.learning_loop import LearningLoop

    loop = LearningLoop(interval=999)
    return loop


class TestLearningLoopClass:
    def test_status_initially_not_running(self, learning_loop):
        s = learning_loop.status
        assert s["running"] is False
        assert s["key"] == "learning-loop"

    @pytest.mark.anyio
    async def test_start_stop(self, learning_loop):
        learning_loop.start()
        assert learning_loop._running is True
        await learning_loop.stop()
        assert learning_loop._running is False

    @pytest.mark.anyio
    async def test_double_start(self, learning_loop):
        learning_loop.start()
        learning_loop.start()
        await learning_loop.stop()

    def test_status_with_scores(self, learning_loop):
        import time

        learning_loop._scores.append({"score": 7, "ts": time.time()})
        s = learning_loop.status
        assert s["obs_count"] == 1
        assert s["avg_score_15min"] is not None

    def test_get_learning_history_with_data(self, learning_loop, learning_db):
        from app.core.learning_loop import _record_learning_event

        _record_learning_event("test_event", "test detail", score_before=5.0, score_after=7.0)
        history = learning_loop.get_learning_history(limit=5)
        assert len(history) >= 1
        assert history[0]["event_type"] == "test_event"

    def test_get_prompt_history_by_component(self, learning_loop, learning_db):
        from app.core.learning_loop import _save_prompt

        _save_prompt("consciousness", "prompt v1", avg_score=7.0)
        history = learning_loop.get_prompt_history(component="consciousness")
        assert len(history) >= 1
        assert history[0]["component"] == "consciousness"

    def test_get_prompt_history_all(self, learning_loop, learning_db):
        from app.core.learning_loop import _save_prompt

        _save_prompt("critic", "critic prompt", avg_score=8.0)
        history = learning_loop.get_prompt_history()
        assert len(history) >= 1
