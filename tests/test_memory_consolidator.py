"""Tests for MemoryConsolidator — temporal graph + importance scoring."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from app.core.memory_consolidator import (
    _ensure_tables,
    _find_patterns,
    _get_top_memories,
    _upsert_edge,
    _upsert_node,
)


@pytest.fixture
def memory_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    monkeypatch.setattr("app.core.memory_consolidator._MEMORY_DB", db_path)
    _ensure_tables()
    yield db_path
    os.unlink(db_path)


class TestDbOperations:
    def test_ensure_tables_creates_schema(self, memory_db):
        con = sqlite3.connect(memory_db)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        con.close()
        assert "memory_nodes" in tables
        assert "memory_edges" in tables
        assert "prompt_versions" in tables

    def test_upsert_node_creates(self, memory_db):
        _upsert_node("focus", "focus:debug", "debug", importance=1.0)
        con = sqlite3.connect(memory_db)
        row = con.execute("SELECT key, value, importance, count FROM memory_nodes WHERE node_type='focus'").fetchone()
        con.close()
        assert row is not None
        assert row[0] == "focus:debug"
        assert row[2] == 1.0
        assert row[3] == 1

    def test_upsert_node_increments(self, memory_db):
        _upsert_node("focus", "focus:debug", "debug", importance=1.0)
        _upsert_node("focus", "focus:debug", "debug", importance=2.0)
        con = sqlite3.connect(memory_db)
        row = con.execute("SELECT count, importance FROM memory_nodes WHERE node_type='focus' AND key='focus:debug'").fetchone()
        con.close()
        assert row[0] == 2
        assert row[1] > 1.0

    def test_upsert_node_with_metadata(self, memory_db):
        meta = '{"source":"critic","score":7}'
        _upsert_node("score", "score:1", "7", importance=0.7, meta=meta)
        con = sqlite3.connect(memory_db)
        row = con.execute("SELECT metadata FROM memory_nodes WHERE key='score:1'").fetchone()
        con.close()
        assert row[0] == meta

    def test_upsert_edge_creates(self, memory_db):
        _upsert_edge("emotion:calm", "focus:debug", "transition")
        con = sqlite3.connect(memory_db)
        row = con.execute("SELECT source_key, target_key, relation, count FROM memory_edges").fetchone()
        con.close()
        assert row[0] == "emotion:calm"
        assert row[1] == "focus:debug"
        assert row[3] == 1

    def test_upsert_edge_increments(self, memory_db):
        _upsert_edge("emotion:calm", "focus:debug", "transition")
        _upsert_edge("emotion:calm", "focus:debug", "transition")
        con = sqlite3.connect(memory_db)
        row = con.execute("SELECT count FROM memory_edges WHERE source_key='emotion:calm' AND target_key='focus:debug'").fetchone()
        con.close()
        assert row[0] == 2

    def test_find_patterns(self, memory_db):
        # _find_patterns 'critic_observed' (emotion->focus) edge'lerini okur; prefix soyulur.
        _upsert_edge("emotion:calm", "focus:debug", "critic_observed")
        _upsert_edge("emotion:calm", "focus:debug", "critic_observed")
        _upsert_edge("emotion:calm", "focus:debug", "critic_observed")
        patterns = _find_patterns()
        assert len(patterns) >= 1
        assert patterns[0]["emotion"] == "calm"
        assert patterns[0]["focus"] == "debug"
        assert patterns[0]["count"] >= 3

    def test_find_patterns_below_threshold(self, memory_db):
        _upsert_edge("emotion:calm", "focus:idle", "critic_observed")
        _upsert_edge("emotion:calm", "focus:idle", "critic_observed")
        patterns = _find_patterns()
        matching = [p for p in patterns if p["emotion"] == "calm" and p["focus"] == "idle"]
        assert len(matching) == 0

    def test_get_top_memories(self, memory_db):
        _upsert_node("focus", "focus:a", "a", importance=3.0)
        _upsert_node("focus", "focus:b", "b", importance=5.0)
        _upsert_node("focus", "focus:c", "c", importance=1.0)
        top = _get_top_memories(limit=2)
        assert len(top) == 2
        assert top[0]["importance"] >= top[1]["importance"]

    def test_get_top_memories_empty_db(self, memory_db):
        assert _get_top_memories() == []

    def test_upsert_node_max_nodes_cleanup_no_crash(self, monkeypatch, memory_db):
        monkeypatch.setattr("app.core.memory_consolidator._MAX_MEMORY_NODES", 3)
        monkeypatch.setattr("app.core.memory_consolidator._LOW_SCORE_CLEANUP_AFTER", 0)
        con = sqlite3.connect(memory_db)
        sql = "INSERT INTO memory_nodes (node_type, key, value, importance, last_seen) VALUES (?, ?, ?, ?, datetime('now', '-2 hours'))"
        con.execute(sql, ("focus", "focus:old0", "old0", 0.1))
        con.execute(sql, ("focus", "focus:old1", "old1", 0.1))
        con.execute(sql, ("focus", "focus:old2", "old2", 0.1))
        con.commit()
        con.close()

        for i in range(3):
            _upsert_node("focus", f"focus:new{i}", f"new{i}", importance=1.0)

        con = sqlite3.connect(memory_db)
        remaining = [r[0] for r in con.execute("SELECT key FROM memory_nodes ORDER BY key").fetchall()]
        con.close()
        assert len(remaining) <= 3

    def test_error_handling_bad_db_path(self, monkeypatch):
        # _MEMORY_DB'yi ERISILEMEZ bir yola isaretle -> sqlite hatasi -> graceful degrade
        # (try/except: log.warning + bos donus), crash YOK. Onceki hali fixture'siz'di =>
        # DEFAULT prod claude_memory.db'ye yaziyordu (test-kirliligi: key/val + a/b/c, count=28)
        # VE _find_patterns artik critic_observed okudugundan prod-pattern'leri donup
        # 'assert patterns == []'i kiriyordu. Bad-path monkeypatch ikisini de cozer.
        monkeypatch.setattr("app.core.memory_consolidator._MEMORY_DB", "/nonexistent/dir/nope.db")
        _upsert_node("focus", "key", "val")
        _upsert_edge("a", "b", "c")
        patterns = _find_patterns()
        assert patterns == []


@pytest.fixture
def consolidator(memory_db):
    from app.core.memory_consolidator import MemoryConsolidator

    c = MemoryConsolidator(interval=999)
    return c


class TestMemoryConsolidatorClass:
    def test_status_initially_not_running(self, consolidator):
        s = consolidator.status
        assert s["running"] is False
        assert s["key"] == "memory-consolidator"

    @pytest.mark.anyio
    async def test_start_stop(self, consolidator):
        consolidator.start()
        assert consolidator._running is True
        await consolidator.stop()
        assert consolidator._running is False

    @pytest.mark.anyio
    async def test_double_start(self, consolidator):
        consolidator.start()
        consolidator.start()
        await consolidator.stop()

    @pytest.mark.anyio
    async def test_last_run_set_by_loop(self, consolidator):
        # Regresyon (dashboard ebedi "—"): last_run _last_state["ts"]'ten okunuyordu ama "ts" hiç
        # yazılmıyordu → aktif ajan hiç çalışmamış görünüyordu. Loop-tick ISO-str damgalamalı.
        import asyncio
        from datetime import datetime

        assert consolidator.status["last_run"] is None
        consolidator.start()
        await asyncio.sleep(0.05)
        try:
            lr = consolidator.status["last_run"]
            assert isinstance(lr, str)
            datetime.fromisoformat(lr)  # geçerli ISO-8601
        finally:
            await consolidator.stop()

    def test_get_top_memories_via_class(self, consolidator):
        from app.core.memory_consolidator import _upsert_node

        _upsert_node("focus", "focus:test", "test", importance=5.0)
        top = consolidator.get_top_memories(limit=5)
        assert len(top) >= 1

    def test_get_patterns_empty(self, consolidator):
        assert consolidator.get_patterns() == []

    @pytest.mark.anyio
    async def test_on_thought_no_payload(self, consolidator):
        from app.core.agent_bus import Event

        event = Event(type="thought:new", source="test", payload={})
        await consolidator._on_thought(event)

    @pytest.mark.anyio
    async def test_on_thought_with_payload(self, consolidator, monkeypatch):
        from app.core.memory_consolidator import _ensure_tables

        _ensure_tables()
        from app.core.agent_bus import Event

        event = Event(
            type="thought:new",
            source="consciousness",
            payload={
                "thought_id": 1,
                "thought": {"focus": "debug", "emotion": "focused", "content": "working on bug fix"},
            },
        )
        await consolidator._on_thought(event)
        assert consolidator._last_state.get("focus") == "debug"

    @pytest.mark.anyio
    async def test_on_thought_builds_transition_edge(self, consolidator, memory_db):
        # Race-fix davranis-korumasi: farkli-focus'lu ardisik thought'lar prev->new transition
        # edge'i uretmeli (atomik read-modify-write prev'i dogru yakalar).
        from app.core.agent_bus import Event

        for i, foc in enumerate(["debug", "idle"]):
            await consolidator._on_thought(
                Event(
                    type="thought:new", source="t", payload={"thought_id": i, "thought": {"focus": foc, "emotion": "calm", "content": "x"}}
                )
            )
        con = sqlite3.connect(memory_db)
        edge = con.execute("SELECT source_key, target_key FROM memory_edges WHERE relation='transition'").fetchone()
        con.close()
        assert edge == ("debug", "idle")
        assert consolidator._last_state["focus"] == "idle"

    @pytest.mark.anyio
    async def test_on_thought_concurrent_state_consistent(self, consolidator, memory_db):
        # Race-fix (runtime-review #169): es-zamanli _on_thought'lar (gather) _last_state'i bozmamali
        # ne de crash olmali. Atomik read-modify-write sayesinde final-state tutarli (iki anahtar da
        # var, focus degeri girdilerden biri — yarim/karisik degil).
        import asyncio

        from app.core.agent_bus import Event

        focuses = [f"f{i}" for i in range(20)]
        await asyncio.gather(
            *[
                consolidator._on_thought(
                    Event(
                        type="thought:new",
                        source="t",
                        payload={"thought_id": i, "thought": {"focus": foc, "emotion": f"e{i}", "content": "x"}},
                    )
                )
                for i, foc in enumerate(focuses)
            ]
        )
        assert set(consolidator._last_state.keys()) == {"focus", "emotion"}
        assert consolidator._last_state["focus"] in focuses

    @pytest.mark.anyio
    async def test_on_critic_score(self, consolidator, monkeypatch):
        from app.core.agent_bus import Event

        event = Event(
            type="critic:score",
            source="critic",
            payload={
                "thought_id": 1,
                "score": 8,
                "thought_focus": "debug",
                "thought_emotion": "focused",
                "is_repetitive": False,
                "boredom_issues": [],
                "completeness_note": "",
                "actionability_note": "",
                "consistency_note": "",
            },
        )
        await consolidator._on_critic_score(event)

    @pytest.mark.anyio
    async def test_on_critic_score_low_score(self, consolidator):
        from app.core.agent_bus import Event

        event = Event(
            type="critic:score",
            source="critic",
            payload={
                "thought_id": 2,
                "score": 3,
                "thought_focus": "idle",
                "thought_emotion": "calm",
                "is_repetitive": True,
                "boredom_issues": [],
            },
        )
        await consolidator._on_critic_score(event)

    def test_status_after_state_change(self, consolidator):
        consolidator._last_state = {"focus": "debug", "emotion": "focused"}
        s = consolidator.status
        assert s["last_state"]["focus"] == "debug"

    @pytest.mark.anyio
    async def test_stop_when_not_running(self, consolidator):
        await consolidator.stop()
        assert consolidator._running is False
