"""Tests for CriticAgent — thought quality scoring and evaluation."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from app.core.critic_agent import _check_boredom, _check_content_repetition


class TestCheckBoredom:
    def test_empty_thoughts(self):
        assert _check_boredom([]) == []

    def test_focus_repetition_triggers(self):
        thoughts = [
            {"focus": "debug", "emotion": "calm"},
            {"focus": "debug", "emotion": "calm"},
            {"focus": "debug", "emotion": "calm"},
            {"focus": "debug", "emotion": "calm"},
            {"focus": "debug", "emotion": "calm"},
            {"focus": "idle", "emotion": "calm"},
        ]
        issues = _check_boredom(thoughts)
        assert any("debug" in i for i in issues)

    def test_calm_emotion_stuck(self):
        thoughts = [
            {"focus": "a", "emotion": "calm"},
            {"focus": "b", "emotion": "calm"},
            {"focus": "c", "emotion": "calm"},
            {"focus": "d", "emotion": "calm"},
            {"focus": "e", "emotion": "calm"},
        ]
        issues = _check_boredom(thoughts)
        assert any("calm" in i for i in issues)

    def test_below_threshold_no_issues(self):
        thoughts = [
            {"focus": "debug", "emotion": "focused"},
            {"focus": "idle", "emotion": "calm"},
        ]
        assert _check_boredom(thoughts) == []

    def test_missing_focus_defaults_unknown(self):
        thoughts = [
            {"emotion": "calm"},
            {"emotion": "calm"},
            {"focus": "debug", "emotion": "focused"},
        ]
        issues = _check_boredom(thoughts)
        assert isinstance(issues, list)


class TestCheckContentRepetition:
    def test_no_content(self):
        assert _check_content_repetition("", []) is False

    def test_repetitive_content(self):
        recent = [{"content": "the quick brown fox jumps over the lazy dog"}]
        assert _check_content_repetition("the quick brown fox jumps over the lazy dog and then runs", recent) is True

    def test_different_content(self):
        recent = [{"content": "the quick brown fox jumps over the lazy dog"}]
        assert _check_content_repetition("completely unrelated topic here", recent) is False

    def test_empty_recent_list(self):
        assert _check_content_repetition("hello world", []) is False

    def test_recent_with_no_content(self):
        recent = [{"emotion": "calm"}]
        assert _check_content_repetition("hello world", recent) is False


class TestCriticAgentSync:
    def test_count_recent(self):
        from app.core.critic_agent import _count_recent

        thoughts = [
            {"focus": "debug", "emotion": "calm"},
            {"focus": "debug", "emotion": "focused"},
            {"focus": "idle", "emotion": "calm"},
        ]
        assert _count_recent("emotion", "calm", thoughts) == 2
        assert _count_recent("emotion", "angry", thoughts) == 0


class TestGetRecentThoughts:
    def test_get_recent_thoughts_empty_db(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
            con.close()
            from app.core.critic_agent import _get_recent_thoughts

            result = _get_recent_thoughts(limit=10)
            assert result == []
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_get_recent_thoughts_with_data(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
            con.execute(
                "INSERT INTO thoughts (id, timestamp, focus, emotion, content) VALUES (1, '2026-01-01', 'debug', 'calm', 'test thought')"
            )
            con.commit()
            con.close()
            from app.core.critic_agent import _get_recent_thoughts

            result = _get_recent_thoughts(limit=10, skip_id=0)
            assert len(result) == 1
            assert result[0]["focus"] == "debug"
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_get_recent_thoughts_skip_id(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
            con.execute("INSERT INTO thoughts (id, timestamp, focus, emotion, content) VALUES (1, '2026-01-01', 'debug', 'calm', 't1')")
            con.execute("INSERT INTO thoughts (id, timestamp, focus, emotion, content) VALUES (2, '2026-01-02', 'idle', 'calm', 't2')")
            con.commit()
            con.close()
            from app.core.critic_agent import _get_recent_thoughts

            result = _get_recent_thoughts(limit=10, skip_id=1)
            assert len(result) == 1
            assert result[0]["id"] == 2
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_get_recent_thoughts_no_table(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            from app.core.critic_agent import _get_recent_thoughts

            result = _get_recent_thoughts()
            assert result == []
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


class TestScoreThought:
    def test_score_basic_thought(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
            con.close()
            from app.core.critic_agent import _score_thought

            result = _score_thought({"content": "a valid thought with enough length", "focus": "debug", "emotion": "focused"})
            assert result["score"] >= 1
            assert result["score"] <= 10
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_score_short_thought_deducted(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.critic_agent._CRITIC_DB_PATH", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, timestamp TEXT, focus TEXT, emotion TEXT, content TEXT)")
            con.close()
            from app.core.critic_agent import _score_thought

            result = _score_thought({"content": "short", "focus": "idle", "emotion": "calm"})
            assert result["completeness_note"] == "cok kisa thought"
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


@pytest.fixture
def critic_agent():
    from app.core.critic_agent import CriticAgent

    agent = CriticAgent(interval=999)
    return agent


class TestCriticAgentClass:
    def test_status_initially_not_running(self, critic_agent):
        s = critic_agent.status
        assert s["running"] is False
        assert s["key"] == "critic"
        assert s["agent_type"] == "critic"

    @pytest.mark.anyio
    async def test_start_stop(self, critic_agent):
        critic_agent.start()
        assert critic_agent._running is True
        await critic_agent.stop()
        assert critic_agent._running is False

    @pytest.mark.anyio
    async def test_double_start(self, critic_agent):
        critic_agent.start()
        critic_agent.start()
        await critic_agent.stop()

    def test_status_after_score(self, monkeypatch, critic_agent):
        critic_agent._last_score = {
            "score": 7,
            "is_repetitive": False,
            "boredom_issues": [],
            "completeness_note": "",
            "actionability_note": "",
            "consistency_note": "",
        }
        critic_agent._score_history.append(critic_agent._last_score)
        s = critic_agent.status
        assert s["last_score"]["score"] == 7
        assert s["avg_score"] == 7.0

    @pytest.mark.anyio
    async def test_on_thought_no_payload(self, critic_agent):
        from app.core.agent_bus import Event

        event = Event(type="thought:new", source="test", payload={})
        await critic_agent._on_thought(event)
        assert critic_agent._last_score is None

    @pytest.mark.anyio
    async def test_on_threshold_adjusted(self, critic_agent):
        from app.core.agent_bus import Event

        event = Event(
            type="learning:threshold_adjusted",
            source="learning_loop",
            payload={
                "thresholds": {"boredom_threshold": 3, "content_repeat_threshold": 0.8},
            },
        )
        await critic_agent._on_threshold_adjusted(event)
        from app.core.critic_agent import _CONTENT_REPEAT_THRESHOLD, _FOCUS_BOREDOM_THRESHOLD

        assert _FOCUS_BOREDOM_THRESHOLD == 3
        assert _CONTENT_REPEAT_THRESHOLD == 0.8
