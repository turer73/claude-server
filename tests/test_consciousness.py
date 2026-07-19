"""Tests for ConsciousnessStream (Functionalism Faz 1)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading

import pytest

from app.core.consciousness import (
    ConsciousnessStream,
    _build_content,
    _determine_emotion,
    _determine_focus,
    _ensure_thoughts_table,
    _get_conn,
    _read_active_alerts,
    _read_daily_summary,
    _read_intent_liveness,
    _read_recent_cron_outcomes,
    _read_recent_events,
    _read_spawn_rate,
    _read_spawn_status,
    _read_synthesis_status,
    _read_triage_status,
    _read_unread_notes,
)

# ── Helpers ──────────────────────────────────────


def _state(**overrides: dict) -> dict:
    """Default empty state override pattern."""
    base = {
        "alerts": {"critical_count": 0, "warning_count": 0, "critical_sources": []},
        "events": {"total": 0, "critical": 0},
        "cron_outcomes": {"partial_count": 0, "fail_count": 0, "partial_jobs": [], "fail_jobs": []},
        "metrics": {},
        "spawn_status": {"poison_count": 0, "pending_count": 0},
        "notes": {"unread": 0},
        "llm": {"total": 0},
        "storage": {},
        "devops": {},
        "spawn_rate": {},
        "synthesis": {},
        "intent_liveness": {},
        "daily_summary": {},
        "triage": {},
    }
    base.update(overrides)
    return base


# ── _determine_focus ─────────────────────────────


class TestDetermineFocus:
    def test_idle(self):
        assert _determine_focus(_state()) == "idle"

    def test_alert_critical(self):
        s = _state(alerts={"critical_count": 2, "warning_count": 0, "critical_sources": ["sys"]})
        assert _determine_focus(s) == "alert:sys"

    def test_alert_critical_no_sources(self):
        s = _state(alerts={"critical_count": 1, "warning_count": 0, "critical_sources": []})
        assert _determine_focus(s) == "alert:critical"

    def test_cron_fail(self):
        s = _state(cron_outcomes={"fail_count": 1, "partial_count": 0, "partial_jobs": [], "fail_jobs": ["backup"]})
        assert _determine_focus(s) == "cron:fail"

    def test_cron_partial(self):
        s = _state(cron_outcomes={"partial_count": 2, "fail_count": 0, "partial_jobs": ["health"], "fail_jobs": []})
        assert _determine_focus(s) == "cron:partial"

    def test_spawn_poison(self):
        s = _state(spawn_status={"poison_count": 1, "pending_count": 0})
        assert _determine_focus(s) == "spawn:poison"

    def test_spawn_pending(self):
        s = _state(spawn_status={"poison_count": 0, "pending_count": 3})
        assert _determine_focus(s) == "spawn:pending"

    def test_metric_cpu(self):
        s = _state(metrics={"cpu": 85})
        assert _determine_focus(s) == "metric:cpu"

    def test_metric_memory(self):
        s = _state(metrics={"cpu": 50, "memory": 90})
        assert _determine_focus(s) == "metric:memory"

    def test_event_critical(self):
        s = _state(events={"total": 5, "critical": 1})
        assert _determine_focus(s) == "event:recent"

    def test_priority_alert_wins(self):
        s = _state(
            alerts={"critical_count": 1, "warning_count": 0, "critical_sources": ["fw"]},
            spawn_status={"poison_count": 3, "pending_count": 0},
        )
        assert _determine_focus(s) == "alert:fw"


# ── _determine_emotion ───────────────────────────


class TestDetermineEmotion:
    def test_calm(self):
        assert _determine_emotion(_state()) == "calm"

    def test_concerned_crit(self):
        s = _state(alerts={"critical_count": 1, "warning_count": 0, "critical_sources": []})
        assert _determine_emotion(s) == "concerned"

    def test_concerned_fail(self):
        s = _state(cron_outcomes={"fail_count": 2, "partial_count": 0, "partial_jobs": [], "fail_jobs": ["x"]})
        assert _determine_emotion(s) == "concerned"

    def test_restless_partial_3(self):
        s = _state(cron_outcomes={"partial_count": 3, "fail_count": 0, "partial_jobs": ["a", "b", "c"], "fail_jobs": []})
        assert _determine_emotion(s) == "restless"

    def test_restless_poison(self):
        s = _state(spawn_status={"poison_count": 2, "pending_count": 0})
        assert _determine_emotion(s) == "restless"

    def test_busy_cpu(self):
        s = _state(metrics={"cpu": 90})
        assert _determine_emotion(s) == "busy"

    def test_busy_events(self):
        s = _state(events={"total": 15, "critical": 0})
        assert _determine_emotion(s) == "busy"


# ── _build_content ───────────────────────────────


class TestBuildContent:
    def test_empty(self):
        assert _build_content(_state(), "idle") == "her sey sakin"

    def test_with_alerts(self):
        s = _state(alerts={"critical_count": 2, "warning_count": 1, "critical_sources": ["fw", "db"]})
        content = _build_content(s, "alert:fw")
        assert "2 kritik" in content
        assert "1 uyari" in content

    def test_with_cron_partial(self):
        s = _state(cron_outcomes={"partial_count": 2, "fail_count": 0, "partial_jobs": ["health"], "fail_jobs": []})
        content = _build_content(s, "cron:partial")
        assert "cron partial" in content

    def test_with_metrics(self):
        s = _state(metrics={"cpu": 75.3})
        content = _build_content(s, "idle")
        assert "CPU %75" in content or "CPU" in content

    def test_with_notes(self):
        s = _state(notes={"unread": 5})
        content = _build_content(s, "idle")
        assert "5 okunmamis" in content

    def test_with_llm(self):
        s = _state(llm={"total": 8})
        content = _build_content(s, "idle")
        assert "LLM cagrisi" in content

    def test_with_poison(self):
        s = _state(spawn_status={"poison_count": 1, "pending_count": 2})
        content = _build_content(s, "spawn:poison")
        assert "poison" in content
        assert "retry" in content


# ── API endpoint tests ───────────────────────────


@pytest.fixture
async def consciousness_client(app):
    """Client with ConsciousnessStream initialized (not running). No default headers."""
    from httpx import ASGITransport, AsyncClient

    from app.core.consciousness import ConsciousnessStream

    stream = ConsciousnessStream(interval=300)
    app.state.consciousness_stream = stream
    stream.start()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await stream.stop()


class TestApiStatus:
    async def test_status(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["thought_count"] >= 0
        assert "emotion" in data

    async def test_status_no_auth(self, consciousness_client):
        resp = await consciousness_client.get("/api/v1/consciousness/status")
        assert resp.status_code in (401, 403)

    async def test_stream(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/stream", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "thoughts" in data
        assert "count" in data

    async def test_stream_with_limit(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/stream?limit=5", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] <= 5

    async def test_self_model(self, consciousness_client, auth_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/self", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "emotion" in data
        assert "focus" in data
        assert "state" in data

    async def test_self_no_auth(self, consciousness_client):
        resp = await consciousness_client.get("/api/v1/consciousness/self")
        assert resp.status_code in (401, 403)

    async def test_status_memory_key(self, consciousness_client, memory_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/status", headers=memory_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True

    async def test_stream_memory_key(self, consciousness_client, memory_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/stream", headers=memory_headers)
        assert resp.status_code == 200
        assert "thoughts" in resp.json()

    async def test_self_memory_key(self, consciousness_client, memory_headers):
        resp = await consciousness_client.get("/api/v1/consciousness/self", headers=memory_headers)
        assert resp.status_code == 200
        assert "emotion" in resp.json()


class TestApiNoStream:
    """API responses when stream is not initialized."""

    async def test_status_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    async def test_stream_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/stream", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["error"] == "not_initialized"

    async def test_self_not_initialized(self, client, auth_headers):
        resp = await client.get("/api/v1/consciousness/self", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["error"] == "not_initialized"


# ── DB reader / persistence tests (temp SQLite files) ────────────────


class TestDbReaders:
    """Integration-style tests with temporary SQLite files."""

    def _create_server_db(self, path: str) -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY,"
            " severity TEXT, source TEXT, message TEXT, timestamp TEXT, resolved INTEGER DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, type TEXT, source TEXT, severity TEXT, title TEXT, timestamp TEXT)"
        )
        con.execute("CREATE TABLE IF NOT EXISTS cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, rc INTEGER, timestamp TEXT)")
        con.commit()
        return con

    def _create_memory_db(self, path: str) -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT,"
            " content TEXT, read INTEGER DEFAULT 0, status TEXT DEFAULT 'active', timestamp TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS spawn_failures (id INTEGER PRIMARY KEY,"
            " note_id INTEGER, title TEXT, attempt_num INTEGER, status TEXT)"
        )
        con.commit()
        return con

    def test_get_conn_error(self):
        assert _get_conn("/nonexistent/path/db.sqlite") is None

    def test_ensure_thoughts_table_creates_schema(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            _ensure_thoughts_table()
            con = sqlite3.connect(db_path)
            tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='thoughts'").fetchall()
            con.close()
            assert len(tables) == 1
        finally:
            os.unlink(db_path)

    def test_read_active_alerts_aggregation(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            con = self._create_server_db(db_path)
            con.execute(
                "INSERT INTO alerts (severity, source, message, timestamp) VALUES ('critical', 'sys', 'disk full', datetime('now'))"
            )
            con.execute("INSERT INTO alerts (severity, source, message, timestamp) VALUES ('warning', 'cpu', 'high load', datetime('now'))")
            con.execute("INSERT INTO alerts (severity, source, message, timestamp) VALUES ('critical', 'fw', 'port scan', datetime('now'))")
            con.commit()
            con.close()
            result = _read_active_alerts()
            assert result["critical_count"] == 2
            assert result["warning_count"] == 1
            assert "sys" in result["critical_sources"]
            assert "fw" in result["critical_sources"]
        finally:
            os.unlink(db_path)

    def test_read_recent_events_aggregation(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            con = self._create_server_db(db_path)
            for i in range(35):
                sev = "critical" if i < 3 else "info"
                con.execute(
                    "INSERT INTO events (type, source, severity, title, timestamp) VALUES (?, 'test', ?, ?, datetime('now'))",
                    ("alert", sev, f"event-{i}"),
                )
            con.commit()
            con.close()
            result = _read_recent_events(minutes=1440)
            assert result["critical"] == 3
            assert result["total"] == 30
        finally:
            os.unlink(db_path)

    def test_read_cron_outcomes_aggregation(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            con = self._create_server_db(db_path)
            con.execute("INSERT INTO cron_outcomes (job, result, rc, timestamp) VALUES ('backup', 'ok', 0, datetime('now'))")
            con.execute("INSERT INTO cron_outcomes (job, result, rc, timestamp) VALUES ('backup', 'fail', 1, datetime('now'))")
            con.execute("INSERT INTO cron_outcomes (job, result, rc, timestamp) VALUES ('health', 'partial', 2, datetime('now'))")
            con.execute("INSERT INTO cron_outcomes (job, result, rc, timestamp) VALUES ('sync', 'ok', 0, datetime('now'))")
            con.commit()
            con.close()
            result = _read_recent_cron_outcomes(minutes=1440)
            assert result["total"] == 4
            assert result["fail_count"] == 1
            assert result["partial_count"] == 1
            assert "backup" in result["fail_jobs"]
            assert "health" in result["partial_jobs"]
        finally:
            os.unlink(db_path)

    def test_read_spawn_status_table_missing(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            sqlite3.connect(db_path).close()
            result = _read_spawn_status()
            assert result.get("table_missing") is True
            assert result["poison_count"] == 0
        finally:
            os.unlink(db_path)

    def test_read_spawn_status_with_data(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = self._create_memory_db(db_path)
            con.execute("INSERT INTO spawn_failures (note_id, title, attempt_num, status) VALUES (1, 'test', 3, 'poison')")
            con.execute("INSERT INTO spawn_failures (note_id, title, attempt_num, status) VALUES (2, 'retry-me', 1, 'pending_retry')")
            con.commit()
            con.close()
            result = _read_spawn_status()
            assert result["poison_count"] == 1
            assert result["pending_count"] == 1
            assert result["total"] == 2
        finally:
            os.unlink(db_path)

    def test_read_unread_notes_with_status_column(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = self._create_memory_db(db_path)
            con.execute("INSERT INTO notes (title, content, read, status) VALUES ('a', 'x', 0, 'active')")
            con.execute("INSERT INTO notes (title, content, read, status) VALUES ('b', 'y', 0, 'archived')")
            con.execute("INSERT INTO notes (title, content, read, status) VALUES ('c', 'z', 1, 'active')")
            con.commit()
            con.close()
            result = _read_unread_notes()
            assert result["unread"] == 1
        finally:
            os.unlink(db_path)

    def test_read_unread_notes_without_status_column(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, content TEXT, read INTEGER DEFAULT 0, timestamp TEXT)")
            con.execute("INSERT INTO notes (title, content, read) VALUES ('a', 'x', 0)")
            con.execute("INSERT INTO notes (title, content, read) VALUES ('b', 'y', 0)")
            con.execute("INSERT INTO notes (title, content, read) VALUES ('c', 'z', 1)")
            con.commit()
            con.close()
            result = _read_unread_notes()
            assert result["unread"] == 2
        finally:
            os.unlink(db_path)

    def test_store_and_read_thought(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            _ensure_thoughts_table()
            stream = ConsciousnessStream(interval=999)
            thought = {
                "timestamp": "2026-01-01T00:00:00",
                "focus": "idle",
                "emotion": "calm",
                "content": "her sey sakin",
                "source_data": '{"test":1}',
                "is_deep": 0,
            }
            thought_id = stream._store_thought(thought)
            thoughts = stream.get_recent_thoughts(limit=10)
            assert thought_id == thoughts[0]["id"]
            assert len(thoughts) == 1
            assert thoughts[0]["focus"] == "idle"
            assert thoughts[0]["emotion"] == "calm"
            assert thoughts[0]["content"] == "her sey sakin"
        finally:
            os.unlink(db_path)

    async def test_run_loop_stores_in_worker_and_publishes_on_main_loop(self, tmp_path, monkeypatch):
        """Regression: DB work stays in a thread; both bus events run on and are awaited by the main loop."""
        from app.core import consciousness as consciousness_module
        from app.core.agent_bus import AgentBus

        db_path = tmp_path / "thoughts.db"
        monkeypatch.setattr(consciousness_module, "MEMORY_DB", str(db_path))
        _ensure_thoughts_table()

        stream = ConsciousnessStream(interval=0)
        shallow = {
            "timestamp": "2026-01-01T00:00:00",
            "focus": "idle",
            "emotion": "calm",
            "content": "normal thought",
            "source_data": "{}",
            "is_deep": 0,
        }
        deep = {
            "timestamp": "2026-01-01T00:01:00",
            "focus": "introspection",
            "emotion": "calm",
            "content": "deep thought",
            "source_data": "{}",
            "is_deep": 1,
        }
        monkeypatch.setattr(stream, "_think", lambda: shallow)
        monkeypatch.setattr(stream, "_think_deep", lambda: deep)

        main_loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        store_threads: list[int] = []
        order: list[str] = []
        original_store = stream._store_thought

        def recording_store(thought):
            store_threads.append(threading.get_ident())
            order.append(f"store:{'deep' if thought.get('is_deep') else 'new'}")
            return original_store(thought)

        monkeypatch.setattr(stream, "_store_thought", recording_store)

        bus = AgentBus()
        monkeypatch.setattr(consciousness_module, "get_bus", lambda: bus)
        received = []

        async def capture(event):
            await asyncio.sleep(0)
            received.append((event, asyncio.get_running_loop(), threading.get_ident()))
            order.append(f"publish:{'deep' if event.type == 'thought:deep' else 'new'}")
            if len(received) == 2:
                stream._running = False

        bus.subscribe("thought:new", capture)
        bus.subscribe("thought:deep", capture)
        stream._llm_timer = consciousness_module.LLM_INTERVAL
        stream._running = True

        await asyncio.wait_for(stream._run_loop(), timeout=2)

        assert [item[0].type for item in received] == ["thought:new", "thought:deep"]
        assert [item[0].payload["thought_id"] for item in received] == [1, 2]
        assert all(loop is main_loop and thread_id == main_thread for _, loop, thread_id in received)
        assert store_threads
        assert all(thread_id != main_thread for thread_id in store_threads)
        assert order == ["store:new", "publish:new", "store:deep", "publish:deep"]

    async def test_worker_lock_is_owned_by_the_stream_instance(self, tmp_path, monkeypatch):
        from app.core import consciousness as consciousness_module

        monkeypatch.setattr(consciousness_module, "MEMORY_DB", str(tmp_path / "thoughts.db"))
        _ensure_thoughts_table()
        lock_results = iter([101, None])
        released: list[int | None] = []
        monkeypatch.setattr(consciousness_module, "_try_worker_lock", lambda: next(lock_results))
        monkeypatch.setattr(consciousness_module, "_release_worker_lock", released.append)

        first = ConsciousnessStream(interval=999)
        second = ConsciousnessStream(interval=999)
        forever = asyncio.Event()

        async def wait_forever():
            await forever.wait()

        monkeypatch.setattr(first, "_run_loop", wait_forever)
        first.start()
        second.start()

        assert first.status["running"] is True
        assert second.status["running"] is False
        await second.stop()
        assert released == []

        await first.stop()
        assert released == [101]

    async def test_external_cohort_lock_mode_skips_legacy_lock(self, tmp_path, monkeypatch):
        from app.core import consciousness as consciousness_module

        monkeypatch.setattr(consciousness_module, "MEMORY_DB", str(tmp_path / "thoughts.db"))
        _ensure_thoughts_table()
        monkeypatch.setattr(
            consciousness_module,
            "_try_worker_lock",
            lambda: (_ for _ in ()).throw(AssertionError("legacy lock must not be acquired")),
        )
        stream = ConsciousnessStream(interval=999, manage_worker_lock=False)
        forever = asyncio.Event()

        async def wait_forever():
            await forever.wait()

        monkeypatch.setattr(stream, "_run_loop", wait_forever)
        stream.start()

        assert stream.status["running"] is True
        await stream.stop()

    def test_get_self_model_structure(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            _ensure_thoughts_table()
            scon = sqlite3.connect(db_path)
            scon.executescript("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY, severity TEXT, source TEXT,
                    message TEXT, timestamp TEXT, resolved INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, type TEXT, source TEXT,
                    severity TEXT, title TEXT, timestamp TEXT);
                CREATE TABLE IF NOT EXISTS cron_outcomes (
                    id INTEGER PRIMARY KEY, job TEXT, result TEXT,
                    rc INTEGER, timestamp TEXT);
                CREATE TABLE IF NOT EXISTS metrics_history (
                    id INTEGER PRIMARY KEY, cpu_usage REAL,
                    memory_usage REAL, disk_usage REAL, temperature REAL);
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY, title TEXT, content TEXT,
                    read INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
                    timestamp TEXT);
                CREATE TABLE IF NOT EXISTS spawn_failures (
                    id INTEGER PRIMARY KEY, note_id INTEGER, title TEXT,
                    attempt_num INTEGER, status TEXT);
            """)
            scon.commit()
            scon.close()
            stream = ConsciousnessStream(interval=999)
            model = stream.get_self_model()
            assert "emotion" in model
            assert "focus" in model
            assert "state" in model
            assert "thought_count" in model
        finally:
            os.unlink(db_path)


# ── Faz 2 focus/emotion/content tests ────────────────────────────────


class TestFaz2Focus:
    """Focus signals from new dashboard component readers."""

    def test_spawn_poison_alert(self):
        s = _state(spawn_rate={"poison_alerts": 2})
        assert _determine_focus(s) == "spawn:poison_alert"

    def test_spawn_active(self):
        s = _state(spawn_rate={"spawns_24h": 35})
        assert _determine_focus(s) == "spawn:active"

    def test_spawn_urgent(self):
        s = _state(spawn_rate={"urgent": 3})
        assert _determine_focus(s) == "spawn:urgent"

    def test_intent_liveness(self):
        s = _state(intent_liveness={"count": 2})
        assert _determine_focus(s) == "intent:issue"

    def test_triage_active(self):
        s = _state(triage={"triaged_24h": 8})
        assert _determine_focus(s) == "triage:active"

    def test_devops_busy(self):
        s = _state(devops={"remediation_24h": 5})
        assert _determine_focus(s) == "devops:busy"

    def test_priority_poison_over_spawn_active(self):
        s = _state(
            spawn_status={"poison_count": 1, "pending_count": 0},
            spawn_rate={"spawns_24h": 50, "poison_alerts": 1},
        )
        assert _determine_focus(s) == "spawn:poison"

    def test_priority_alert_over_all_faz2(self):
        s = _state(
            alerts={"critical_count": 1, "warning_count": 0, "critical_sources": ["fw"]},
            intent_liveness={"count": 5},
            triage={"triaged_24h": 10},
            spawn_rate={"spawns_24h": 40},
        )
        assert _determine_focus(s) == "alert:fw"


class TestFaz2Emotion:
    """Emotion signals from new dashboard component readers."""

    def test_poison_alert_concerned(self):
        s = _state(spawn_rate={"poison_alerts": 1})
        assert _determine_emotion(s) == "concerned"

    def test_intent_count_restless(self):
        s = _state(intent_liveness={"count": 3})
        assert _determine_emotion(s) == "restless"

    def test_high_spawn_rate_busy(self):
        s = _state(spawn_rate={"spawns_24h": 35})
        assert _determine_emotion(s) == "busy"


class TestFaz2Content:
    """Content items from new dashboard component readers."""

    def test_with_spawn_rate(self):
        s = _state(spawn_rate={"spawns_24h": 20, "ack": 15, "urgent": 2})
        content = _build_content(s, "spawn:active")
        assert "24h 20 spawn" in content

    def test_with_poison_alerts(self):
        s = _state(spawn_rate={"poison_alerts": 3})
        content = _build_content(s, "spawn:poison_alert")
        assert "3 spawn poison alerti" in content

    def test_with_intent_liveness(self):
        s = _state(intent_liveness={"count": 4})
        content = _build_content(s, "intent:issue")
        assert "4 intent-liveness bulgusu" in content

    def test_with_synthesis(self):
        s = _state(synthesis={"archived_count": 12})
        content = _build_content(s, "idle")
        assert "12 memory archive edilmis" in content

    def test_with_triage(self):
        s = _state(triage={"triaged_24h": 3})
        content = _build_content(s, "triage:active")
        assert "triage 3 bayat kayit" in content

    def test_with_devops_remediation(self):
        s = _state(devops={"remediation_24h": 2})
        content = _build_content(s, "devops:busy")
        assert "2 remediation 24h" in content

    def test_with_daily_summary(self):
        s = _state(daily_summary={"latest": "autonomous-daily-summary-2026-07-07"})
        content = _build_content(s, "idle")
        assert "summary: autonomous-daily-summary-" in content


# ── Faz 2 DB reader tests ────────────────────────────────────────────


class TestFaz2DbReaders:
    """DB reader tests for new Faz 2 functions."""

    def _create_memory_tables(self, con: sqlite3.Connection) -> None:
        con.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, name TEXT, content TEXT, created_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS discoveries (id INTEGER PRIMARY KEY, type TEXT, title TEXT, status TEXT, updated_at TEXT)")
        con.commit()

    def test_read_spawn_rate(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = sqlite3.connect(db_path)
            self._create_memory_tables(con)
            con.execute("INSERT INTO memories (name, created_at) VALUES ('autonomous-spawn-x1', datetime('now', '-1 hour'))")
            con.execute("INSERT INTO memories (name, created_at) VALUES ('autonomous-ack-y1', datetime('now'))")
            con.execute("INSERT INTO memories (name, created_at) VALUES ('autonomous-urgent-z1', datetime('now'))")
            con.commit()
            con.close()
            result = _read_spawn_rate()
            assert result["spawns_24h"] == 1
            assert result["ack"] == 1
            assert result["urgent"] == 1
        finally:
            os.unlink(db_path)

    def test_read_synthesis_status(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            con = sqlite3.connect(db_path)
            self._create_memory_tables(con)
            con.execute("ALTER TABLE memories ADD COLUMN merged_into INTEGER")
            con.commit()
            con.close()
            result = _read_synthesis_status()
            assert result["archived_count"] >= 0
            assert "last_outcome" in result
        finally:
            os.unlink(db_path)

    def test_read_intent_liveness(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.SERVER_DB", db_path)
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, type TEXT, severity TEXT, title TEXT, timestamp TEXT)")
            con.execute(
                "INSERT INTO events (type, severity, title, timestamp)"
                " VALUES ('intent-liveness','critical','dead infra ref',datetime('now'))"
            )
            con.commit()
            con.close()
            result = _read_intent_liveness(minutes=1440)
            assert result["count"] == 1
            assert "dead infra ref" in result["critical_titles"]
        finally:
            os.unlink(db_path)

    def test_read_daily_summary(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = sqlite3.connect(db_path)
            self._create_memory_tables(con)
            con.execute(
                "INSERT INTO memories (name, content, created_at)"
                " VALUES ('autonomous-daily-summary-2026-07-07','test content',datetime('now'))"
            )
            con.commit()
            con.close()
            result = _read_daily_summary()
            assert result["latest"] is not None
            assert "2026-07-07" in result["latest"]
        finally:
            os.unlink(db_path)

    def test_read_triage_status(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = sqlite3.connect(db_path)
            self._create_memory_tables(con)
            con.execute("INSERT INTO discoveries (type, title, status, updated_at) VALUES ('bug', 'old bug', 'obsolete', datetime('now'))")
            con.execute(
                "INSERT INTO discoveries (type, title, status, updated_at) VALUES ('workaround', 'old fix', 'superseded', datetime('now'))"
            )
            con.commit()
            con.close()
            result = _read_triage_status()
            assert result["obsolete_count"] == 1
            assert result["superseded_count"] == 1
        finally:
            os.unlink(db_path)

    def test_read_daily_summary_none(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            monkeypatch.setattr("app.core.consciousness.MEMORY_DB", db_path)
            con = sqlite3.connect(db_path)
            self._create_memory_tables(con)
            con.close()
            result = _read_daily_summary()
            assert result["latest"] is None
        finally:
            os.unlink(db_path)
