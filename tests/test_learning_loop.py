"""Tests for LearningLoop — closed-loop improvement from critic feedback."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import UTC, datetime

import pytest

from app.core.learning_loop import (
    _SCORE_AFTER_DELAY,
    _ensure_learning_table,
    _load_prompt,
    _record_learning_event,
    _save_prompt,
    _update_score_after,
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

    def test_record_learning_event_returns_row_id(self, learning_db):
        # score_after doldurma (flywheel açık-döngü fix): id dönmeli ki caller sonra UPDATE edebilsin.
        event_id = _record_learning_event("threshold_adjustment", "test event", score_before=5.0)
        assert isinstance(event_id, int)
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] is None  # baslangicta NULL

    def test_update_score_after(self, learning_db):
        event_id = _record_learning_event("threshold_adjustment", "test event", score_before=5.0)
        assert _update_score_after(event_id, 7.2) is True
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] == 7.2

    def test_update_score_after_bad_db_no_crash(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        assert _update_score_after(1, 5.0) is False  # patlamamali, basarisizlik-sinyali doner

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
        # Not: eski hali 6 gözlemle _MIN_OBSERVATIONS(10) guard'ına takılıp erken dönüyordu ve
        # hiçbir şey assert etmiyordu — öğrenme yolu fiilen test edilmiyordu. >=10 gözlem + assert.
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        now = time.time()

        scores = [{"score": 7, "ts": now - 3000 + i * 50, "boredom_issues": []} for i in range(7)]
        scores += [
            {"score": 4, "ts": now - 100, "boredom_issues": []},
            {"score": 4, "ts": now - 80, "boredom_issues": []},
            {"score": 4, "ts": now - 60, "boredom_issues": []},
        ]
        loop._scores.extend(scores)

        import asyncio

        asyncio.run(loop._evaluate_and_learn())
        assert loop._learn_count == 1  # 15min(4.0) < 1h - 0.5 → downtrend öğrenmesi

    def test_collapsed_score_triggers_learn(self, learning_db, monkeypatch):
        # Regresyon: eski _MIN_SCORE_BEFORE_LEARN(4) guard'ı avg<4'te öğrenmeyi TAMAMEN bloklardı —
        # modülün amacının tam tersi (kalite çökünce tepkisiz). Guard kalktı, çöküş artık tetikler.
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", learning_db)
        from app.core.learning_loop import LearningLoop

        loop = LearningLoop()
        now = time.time()
        loop._scores.extend({"score": 3, "ts": now - 200 + i * 10, "boredom_issues": []} for i in range(12))

        import asyncio

        asyncio.run(loop._evaluate_and_learn())
        assert loop._learn_count == 1
        assert "boredom_threshold" in loop._current_thresholds

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

    @pytest.mark.anyio
    async def test_last_run_iso_contract(self, learning_loop):
        # Regresyon (dashboard "20623g önce"): last_run epoch-float döndürüyordu, sözleşme ISO-str.
        # Skor varken bile float sızmamalı; loop-tick sonrası ISO string olmalı.
        import asyncio
        import time
        from datetime import datetime

        learning_loop._scores.append({"score": 7, "ts": time.time()})
        assert learning_loop.status["last_run"] is None  # tick öncesi: float skor-ts'i DEĞİL
        learning_loop.start()
        await asyncio.sleep(0.05)
        try:
            lr = learning_loop.status["last_run"]
            assert isinstance(lr, str)
            datetime.fromisoformat(lr)  # geçerli ISO-8601
        finally:
            await learning_loop.stop()

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


class TestScoreAfterResolution:
    """score_after flywheel-fix: learn-event tetiklendiğinde bağımsız bir asyncio-task'e
    zamanlanır (code-review#307-P2: _run_loop'un tick-cadence'ına bağlı DEĞİL — prod'da
    interval=3600s iken tick-tabanlı olsaydı 15dk hedefi saatlik-tick'e kayardı), vadesi
    dolunca o anki 15min-ortalamasıyla DB'de doldurulur, geçici hata retry'lanır."""

    @pytest.mark.anyio
    async def test_learn_registers_pending_and_spawns_task(self, learning_loop):
        now = time.time()
        scores = [{"score": 7, "ts": now - 3000 + i * 50, "boredom_issues": []} for i in range(7)]
        scores += [{"score": 4, "ts": now - t, "boredom_issues": []} for t in (100, 80, 60)]
        learning_loop._scores.extend(scores)

        await learning_loop._evaluate_and_learn()

        assert learning_loop._learn_count == 1
        assert len(learning_loop._pending_score_after) == 1
        assert len(learning_loop._score_after_tasks) == 1
        (due_ts,) = learning_loop._pending_score_after.values()
        assert due_ts >= now + _SCORE_AFTER_DELAY
        for t in learning_loop._score_after_tasks:
            t.cancel()

    @pytest.mark.anyio
    async def test_resolve_score_after_fills_and_clears_pending(self, learning_loop, learning_db):
        event_id = _record_learning_event("threshold_adjustment", "test", score_before=4.0)
        learning_loop._pending_score_after[event_id] = time.time() - 1  # vadesi gecmis
        learning_loop._scores.append({"score": 8, "ts": time.time()})

        await learning_loop._resolve_score_after(event_id, time.time() - 1)

        assert event_id not in learning_loop._pending_score_after
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] == pytest.approx(8.0)

    @pytest.mark.anyio
    async def test_resolve_score_after_waits_for_due_ts(self, learning_loop, learning_db):
        event_id = _record_learning_event("threshold_adjustment", "test", score_before=4.0)
        learning_loop._scores.append({"score": 8, "ts": time.time()})
        start = time.time()

        await learning_loop._resolve_score_after(event_id, time.time() + 0.05)

        assert time.time() - start >= 0.05

    @pytest.mark.anyio
    async def test_resolve_score_after_retries_then_succeeds(self, learning_loop, learning_db, monkeypatch):
        # Ilk deneme gecici-hata (DB-lock benzeri), ikinci deneme basarili -> tek-hatada
        # kalici-NULL birakan eski davranisin regresyonu (code-review#307-P2).
        monkeypatch.setattr("app.core.learning_loop._SCORE_AFTER_RETRY_DELAY", 0.01)
        event_id = _record_learning_event("threshold_adjustment", "test", score_before=4.0)
        learning_loop._pending_score_after[event_id] = time.time() - 1
        learning_loop._scores.append({"score": 8, "ts": time.time()})

        calls = {"n": 0}
        real_update = _update_score_after

        def flaky_update(eid, score):
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            return real_update(eid, score)

        monkeypatch.setattr("app.core.learning_loop._update_score_after", flaky_update)

        await learning_loop._resolve_score_after(event_id, time.time() - 1)

        assert calls["n"] == 2
        assert event_id not in learning_loop._pending_score_after
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] == pytest.approx(8.0)

    @pytest.mark.anyio
    async def test_resolve_score_after_gives_up_after_max_retries(self, learning_loop, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._SCORE_AFTER_RETRY_DELAY", 0.01)
        monkeypatch.setattr("app.core.learning_loop._update_score_after", lambda eid, score: False)
        event_id = _record_learning_event("threshold_adjustment", "test", score_before=4.0)
        learning_loop._pending_score_after[event_id] = time.time() - 1
        learning_loop._scores.append({"score": 8, "ts": time.time()})

        await learning_loop._resolve_score_after(event_id, time.time() - 1)

        # pes edince pending'den DUSMEZ (kalici-kayip yerine "hala cozulmedi" izi kalir)
        assert event_id in learning_loop._pending_score_after
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] is None

    @pytest.mark.anyio
    async def test_resolve_score_after_without_window_data_retries(self, learning_loop, learning_db, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._SCORE_AFTER_RETRY_DELAY", 0.01)
        event_id = _record_learning_event("threshold_adjustment", "test", score_before=4.0)
        learning_loop._pending_score_after[event_id] = time.time() - 1
        # _scores bos -> 15min penceresi None, retry'lar tukenmeli

        await learning_loop._resolve_score_after(event_id, time.time() - 1)

        assert event_id in learning_loop._pending_score_after
        con = sqlite3.connect(learning_db)
        row = con.execute("SELECT score_after FROM learning_events WHERE id=?", (event_id,)).fetchone()
        con.close()
        assert row[0] is None


class TestPendingScoreAfterRecovery:
    """Restart-recovery (code-review#307-P2): süreç yeniden-başlarsa in-memory pending-state
    kaybolmasın diye score_after NULL kalan satırlar created_at'tan geri-hesaplanır."""

    def test_load_pending_recovers_row(self, learning_db):
        con = sqlite3.connect(learning_db)
        con.execute(
            "INSERT INTO learning_events (event_type, detail, score_before, created_at) VALUES (?, ?, ?, ?)",
            ("threshold_adjustment", "d", 5.0, "2026-01-01 00:00:00"),
        )
        con.commit()
        event_id = con.execute("SELECT id FROM learning_events").fetchone()[0]
        con.close()

        from app.core.learning_loop import _load_pending_score_after

        pending = _load_pending_score_after()
        assert event_id in pending
        expected_due = datetime(2026, 1, 1, tzinfo=UTC).timestamp() + _SCORE_AFTER_DELAY
        assert pending[event_id] == pytest.approx(expected_due)

    def test_load_pending_ignores_non_threshold_events(self, learning_db):
        _record_learning_event("some_other_event", "d", score_before=5.0)
        from app.core.learning_loop import _load_pending_score_after

        assert _load_pending_score_after() == {}

    def test_load_pending_ignores_already_resolved(self, learning_db):
        _record_learning_event("threshold_adjustment", "d", score_before=5.0, score_after=6.0)
        from app.core.learning_loop import _load_pending_score_after

        assert _load_pending_score_after() == {}

    def test_load_pending_bad_db_no_crash(self, monkeypatch):
        monkeypatch.setattr("app.core.learning_loop._MEMORY_DB", "/nonexistent/testing.sqlite")
        from app.core.learning_loop import _load_pending_score_after

        assert _load_pending_score_after() == {}

    @pytest.mark.anyio
    async def test_start_recovers_and_schedules(self, learning_loop, learning_db):
        con = sqlite3.connect(learning_db)
        con.execute(
            "INSERT INTO learning_events (event_type, detail, score_before, created_at) VALUES (?, ?, ?, datetime('now'))",
            ("threshold_adjustment", "d", 5.0),
        )
        con.commit()
        con.close()

        learning_loop.start()
        try:
            assert len(learning_loop._pending_score_after) == 1
            assert len(learning_loop._score_after_tasks) == 1
        finally:
            await learning_loop.stop()
