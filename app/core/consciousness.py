"""Consciousness Stream — continuous self-narrative loop (Functionalism Faz 1).

Every 15s: reads all system state from 4 DBs → produces structured thought.
Every 5min: also calls qwen2.5:3b for LLM inner monologue from recent thoughts.

Design:
- Follows same pattern as DevOpsAgent in devops_agent.py
- Reads (never writes) to server.db, claude_memory.db, rag_metrics.db
- Writes ONLY to claude_memory.db.thoughts table (append-only stream)
- Fail-safe: any read error → degraded state, never blocks the loop
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import urllib.request
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("consciousness")

_DATA_DIR = "/opt/linux-ai-server/data"
MEMORY_DB = os.environ.get("MEMORY_DB", f"{_DATA_DIR}/claude_memory.db")
SERVER_DB = os.environ.get("DB_PATH", f"{_DATA_DIR}/server.db")
RAG_DB = os.environ.get("RAG_METRICS_DB", f"{_DATA_DIR}/rag_metrics.db")

FAST_INTERVAL = 15
LLM_INTERVAL = 300
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


# ── thoughts table schema ─────────────────────────────────────────────

_THOUGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS thoughts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    focus TEXT NOT NULL,
    emotion TEXT NOT NULL,
    content TEXT NOT NULL,
    source_data TEXT,
    is_deep INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_thoughts_ts ON thoughts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_thoughts_emotion ON thoughts(emotion);
"""


def _ensure_thoughts_table() -> None:
    """Idempotent schema migration — claude_memory.db'ye thoughts tablosu ekle."""
    try:
        con = sqlite3.connect(MEMORY_DB, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_THOUGHTS_SCHEMA)
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("thoughts table ensure failed (non-fatal): %s", e)


def _get_conn(db_path: str) -> sqlite3.Connection | None:
    """Fail-safe connection — any error returns None (loop never breaks)."""
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        return con
    except sqlite3.Error:
        return None


# ── State readers (fail-safe, each returns dict with error info) ──────


def _read_active_alerts() -> dict[str, Any]:
    """server.db.alerts — active (unresolved) alerts grouped by severity."""
    con = _get_conn(SERVER_DB)
    if not con:
        return {"critical_count": -1, "warning_count": -1, "critical_sources": [], "error": "db_unreachable"}
    try:
        rows = con.execute(
            "SELECT severity, source, message, timestamp FROM alerts WHERE resolved=0 ORDER BY id DESC LIMIT 20"
        ).fetchall()
        con.close()
        critical = [dict(r) for r in rows if r["severity"] == "critical"]
        warnings = [dict(r) for r in rows if r["severity"] == "warning"]
        return {
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "critical_sources": [a["source"] for a in critical[:5]],
            "alerts": [dict(r) for r in rows[:10]],
        }
    except sqlite3.Error as e:
        con.close()
        return {"critical_count": -1, "error": str(e)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _read_recent_events(minutes: int = 5) -> dict[str, Any]:
    """server.db.events — recent events by severity."""
    con = _get_conn(SERVER_DB)
    if not con:
        return {"count": 0, "error": "db_unreachable"}
    try:
        rows = con.execute(
            "SELECT type, source, severity, title, timestamp FROM events "
            "WHERE timestamp > datetime('now', ?) ORDER BY id DESC LIMIT 30",
            (f"-{minutes} minutes",),
        ).fetchall()
        con.close()
        critical = [dict(r) for r in rows if r["severity"] == "critical"]
        return {
            "total": len(rows),
            "critical": len(critical),
            "critical_titles": [e["title"] for e in critical[:5]],
            "events": [dict(r) for r in rows[:10]],
        }
    except sqlite3.Error as e:
        con.close()
        return {"count": 0, "error": str(e)}


def _read_recent_cron_outcomes(minutes: int = 1440) -> dict[str, Any]:
    """server.db.cron_outcomes — last 24h cron results, grouped by result."""
    con = _get_conn(SERVER_DB)
    if not con:
        return {"partial_count": 0, "fail_count": 0, "error": "db_unreachable"}
    try:
        rows = con.execute(
            "SELECT job, result, rc, timestamp FROM cron_outcomes "
            "WHERE timestamp > datetime('now', ?) ORDER BY timestamp DESC LIMIT 50",
            (f"-{minutes} minutes",),
        ).fetchall()
        con.close()
        partial = [dict(r) for r in rows if r["result"] == "partial"]
        fails = [dict(r) for r in rows if r["result"] == "fail"]
        return {
            "total": len(rows),
            "partial_count": len(partial),
            "fail_count": len(fails),
            "partial_jobs": list({p["job"] for p in partial}),
            "fail_jobs": list({f["job"] for f in fails}),
            "recent": [dict(r) for r in rows[:10]],
        }
    except sqlite3.Error as e:
        con.close()
        return {"partial_count": 0, "fail_count": 0, "error": str(e)}


def _read_latest_metrics() -> dict[str, Any]:
    """server.db.metrics_history — latest metric snapshot."""
    con = _get_conn(SERVER_DB)
    if not con:
        return {"error": "db_unreachable"}
    try:
        row = con.execute(
            "SELECT cpu_usage, memory_usage, disk_usage, temperature FROM metrics_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
        if row:
            return {"cpu": row["cpu_usage"], "memory": row["memory_usage"], "disk": row["disk_usage"], "temperature": row["temperature"]}
        return {}
    except sqlite3.Error as e:
        con.close()
        return {"error": str(e)}


def _read_spawn_status() -> dict[str, Any]:
    """claude_memory.db.spawn_failures — DLQ and spawn health."""
    con = _get_conn(MEMORY_DB)
    if not con:
        return {"poison_count": -1, "error": "db_unreachable"}
    try:
        # Check if table exists
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spawn_failures'").fetchone()
        if not tables:
            con.close()
            return {"poison_count": 0, "pending_count": 0, "table_missing": True}
        poison = con.execute("SELECT COUNT(*) FROM spawn_failures WHERE status='poison'").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM spawn_failures WHERE status='pending_retry'").fetchone()[0]
        recent = con.execute(
            "SELECT note_id, title, attempt_num, status FROM spawn_failures ORDER BY id DESC LIMIT 5"
        ).fetchall()
        con.close()
        return {
            "poison_count": poison,
            "pending_count": pending,
            "total": poison + pending,
            "recent": [dict(r) for r in recent],
        }
    except sqlite3.Error as e:
        con.close()
        return {"poison_count": -1, "error": str(e)}


def _read_recent_llm_calls(minutes: int = 5) -> dict[str, Any]:
    """rag_metrics.db.llm_calls — recent LLM activity."""
    con = _get_conn(RAG_DB)
    if not con:
        return {"total": 0, "error": "db_unreachable"}
    try:
        rows = con.execute(
            "SELECT task, backend, model, ok, latency_ms FROM llm_calls "
            "WHERE ts > datetime('now', ?) ORDER BY id DESC LIMIT 30",
            (f"-{minutes} minutes",),
        ).fetchall()
        con.close()
        failed = [dict(r) for r in rows if not r["ok"]]
        return {
            "total": len(rows),
            "failed": len(failed),
            "by_task": {t: sum(1 for r in rows if r["task"] == t) for t in {r["task"] for r in rows}},
            "recent": [dict(r) for r in rows[:10]],
        }
    except sqlite3.Error as e:
        con.close()
        return {"total": 0, "error": str(e)}


def _read_unread_notes() -> dict[str, Any]:
    """claude_memory.db.notes — unread count."""
    con = _get_conn(MEMORY_DB)
    if not con:
        return {"unread": -1, "error": "db_unreachable"}
    try:
        unread = con.execute("SELECT COUNT(*) FROM notes WHERE read=0 AND COALESCE(status,'active')='active'").fetchone()[0]
        con.close()
        return {"unread": unread}
    except sqlite3.Error as e:
        con.close()
        return {"unread": -1, "error": str(e)}


# ── Emotion engine (rule-based) ──────────────────────────────────────


def _determine_focus(state: dict) -> str:
    if state["alerts"].get("critical_count", 0) > 0:
        srcs = state["alerts"].get("critical_sources", [])
        return f"alert:{srcs[0]}" if srcs else "alert:critical"
    if state["cron_outcomes"].get("fail_count", 0) > 0:
        return "cron:fail"
    if state["cron_outcomes"].get("partial_count", 0) > 0:
        return "cron:partial"
    if state["spawn_status"].get("poison_count", 0) > 0:
        return "spawn:poison"
    if state["spawn_status"].get("pending_count", 0) > 0:
        return "spawn:pending"
    m = state.get("metrics", {})
    if m.get("cpu", 0) and m["cpu"] > 80:
        return "metric:cpu"
    if m.get("memory", 0) and m["memory"] > 80:
        return "metric:memory"
    if state["events"].get("critical", 0) > 0:
        return "event:recent"
    return "idle"


def _determine_emotion(state: dict, prev_emotion: str | None = None) -> str:
    crit_alerts = state["alerts"].get("critical_count", 0)
    fails = state["cron_outcomes"].get("fail_count", 0)
    partials = state["cron_outcomes"].get("partial_count", 0)
    poison = state["spawn_status"].get("poison_count", 0)

    if crit_alerts > 0 or fails > 0:
        return "concerned"
    if partials >= 3 or poison > 0:
        return "restless"
    cpu = state.get("metrics", {}).get("cpu", 0)
    if cpu and cpu > 80:
        return "busy"
    if state["events"].get("total", 0) > 10:
        return "busy"
    return "calm"


# ── Thought builder ──────────────────────────────────────────────────


def _build_content(state: dict, focus: str) -> str:
    parts = []
    a = state["alerts"]
    if a.get("critical_count", 0) > 0:
        parts.append(f"{a['critical_count']} kritik uyari: {', '.join(a['critical_sources'][:3])}")
    if a.get("warning_count", 0) > 0:
        parts.append(f"{a['warning_count']} uyari")
    co = state["cron_outcomes"]
    if co.get("partial_count", 0) > 0:
        jobs = ", ".join(co.get("partial_jobs", [])[:3])
        parts.append(f"{co['partial_count']} cron partial ({jobs})")
    if co.get("fail_count", 0) > 0:
        parts.append(f"{co['fail_count']} cron fail")
    sf = state["spawn_status"]
    if sf.get("poison_count", 0) > 0:
        parts.append(f"{sf['poison_count']} spawn poison DLQ'da")
    if sf.get("pending_count", 0) > 0:
        parts.append(f"{sf['pending_count']} spawn retry bekliyor")
    m = state.get("metrics", {})
    if m.get("cpu"):
        parts.append(f"CPU %{m['cpu']:.0f}")
    if m.get("memory"):
        parts.append(f"RAM %{m['memory']:.0f}")
    e = state["events"]
    if e.get("critical", 0) > 0:
        parts.append(f"son 5dk {e['critical']} kritik event")
    n = state.get("notes", {})
    if n.get("unread", 0) > 0:
        parts.append(f"{n['unread']} okunmamis not")
    llm = state.get("llm", {})
    if llm.get("total", 0) > 0:
        parts.append(f"son 5dk {llm['total']} LLM cagrisi")
    if not parts:
        return "her sey sakin"
    return " | ".join(parts)


# ── LLM deep thought ─────────────────────────────────────────────────


def _ollama_think(prompt: str, model: str = "qwen2.5:3b", timeout: int = 30) -> str | None:
    """qwen2.5:3b'ye sor sor, yanıt döndür (fail-safe → None)."""
    try:
        req = urllib.request.Request(  # noqa: S310
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False, "temperature": 0.3, "num_predict": 200}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return (data.get("response") or "").strip() or None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        log.warning("deep thought LLM error: %s", e)
        return None


def _build_deep_thought_prompt(recent_thoughts: list[dict], current_state: dict) -> str:
    """Prompt for 5-min inner monologue."""
    focus = current_state.get("_focus", "idle")
    emotion = current_state.get("_emotion", "calm")
    return f"""Su anki durumum:
Odak: {focus}
Duygu: {emotion}
Son durum: {_build_content(current_state, focus)}

Son 5 dakikadaki dusuncelerim:
{chr(10).join(f'- [{t["emotion"]}] {t["content"][:120]}' for t in recent_thoughts[-6:])}

Bu durum hakkinda ne dusunuyorum? (1-2 cumle, ic monolog olarak)"""


# ── ConsciousnessStream class ─────────────────────────────────────────


class ConsciousnessStream:
    """Background loop: read state → think → store thought. Wired like DevOpsAgent."""

    def __init__(self, interval: int = FAST_INTERVAL):
        self._interval = interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._started_at: str | None = None
        self._last_thought: dict | None = None
        self._thought_count = 0
        self._prev_emotion: str | None = None
        self._llm_timer = 0
        self._recent_thoughts: list[dict] = []
        _ensure_thoughts_table()

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "started_at": self._started_at,
            "thought_count": self._thought_count,
            "interval": self._interval,
            "last_thought": self._last_thought,
            "emotion": self._prev_emotion or "unknown",
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(UTC).isoformat()
        self._task = asyncio.create_task(self._run_loop())
        log.info("consciousness stream started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            log.info("consciousness stream stopped")

    @property
    def thought_count(self) -> int:
        return self._thought_count

    async def _run_loop(self) -> None:
        while self._running:
            try:
                thought = await asyncio.to_thread(self._think)
                await asyncio.to_thread(self._store_thought, thought)
                self._last_thought = thought
                self._thought_count += 1
                self._recent_thoughts.append(thought)
                if len(self._recent_thoughts) > 30:
                    self._recent_thoughts.pop(0)

                self._llm_timer += self._interval
                if self._llm_timer >= LLM_INTERVAL:
                    deep = await asyncio.to_thread(self._think_deep)
                    if deep:
                        await asyncio.to_thread(self._store_thought, deep)
                        self._recent_thoughts.append(deep)
                        if len(self._recent_thoughts) > 30:
                            self._recent_thoughts.pop(0)
                    self._llm_timer = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("consciousness tick error: %s", e, exc_info=True)
            await asyncio.sleep(self._interval)

    def _think(self) -> dict:
        state = self._read_all_state()
        focus = _determine_focus(state)
        emotion = _determine_emotion(state, self._prev_emotion)
        self._prev_emotion = emotion
        content = _build_content(state, focus)
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "focus": focus,
            "emotion": emotion,
            "content": content,
            "source_data": json.dumps(state, default=str),
            "is_deep": 0,
        }

    def _read_all_state(self) -> dict:
        return {
            "alerts": _read_active_alerts(),
            "events": _read_recent_events(),
            "cron_outcomes": _read_recent_cron_outcomes(),
            "metrics": _read_latest_metrics(),
            "spawn_status": _read_spawn_status(),
            "llm": _read_recent_llm_calls(),
            "notes": _read_unread_notes(),
        }

    def _think_deep(self) -> dict | None:
        recent = self._recent_thoughts[-10:] if self._recent_thoughts else []
        state = self._read_all_state()
        state["_focus"] = _determine_focus(state)
        state["_emotion"] = _determine_emotion(state, self._prev_emotion)
        prompt = _build_deep_thought_prompt(recent, state)
        response = _ollama_think(prompt)
        if not response:
            return None
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "focus": "introspection",
            "emotion": self._prev_emotion or "calm",
            "content": response.strip(),
            "source_data": json.dumps({"prompt_len": len(prompt), "mode": "deep"}, default=str),
            "is_deep": 1,
        }

    def _store_thought(self, thought: dict) -> None:
        try:
            con = sqlite3.connect(MEMORY_DB, timeout=10)
            con.execute(
                "INSERT INTO thoughts (timestamp, focus, emotion, content, source_data, is_deep) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thought["timestamp"],
                    thought["focus"],
                    thought["emotion"],
                    thought["content"],
                    thought.get("source_data"),
                    thought.get("is_deep", 0),
                ),
            )
            con.commit()
            con.close()
        except sqlite3.Error as e:
            log.warning("store thought failed: %s", e)

    def get_recent_thoughts(self, limit: int = 30) -> list[dict]:
        """API consumption: latest thoughts from DB."""
        try:
            con = sqlite3.connect(MEMORY_DB, timeout=10)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, timestamp, focus, emotion, content, is_deep FROM thoughts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def get_self_model(self) -> dict:
        """Current self-model: aggregated state + emotional trend."""
        recent = self.get_recent_thoughts(limit=10)
        emotions = [t["emotion"] for t in recent if t.get("emotion")]
        dominant = max(set(emotions), key=emotions.count) if emotions else "unknown"
        state = self._read_all_state()
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "emotion": dominant,
            "focus": _determine_focus(state),
            "thought_count": self._thought_count,
            "uptime": self._started_at,
            "content": _build_content(state, _determine_focus(state)),
            "state": {
                "alerts": state["alerts"].get("critical_count", 0),
                "warnings": state["alerts"].get("warning_count", 0),
                "cron_partial": state["cron_outcomes"].get("partial_count", 0),
                "cron_fail": state["cron_outcomes"].get("fail_count", 0),
                "spawn_poison": state["spawn_status"].get("poison_count", 0),
                "spawn_pending": state["spawn_status"].get("pending_count", 0),
                "cpu": state["metrics"].get("cpu"),
                "memory": state["metrics"].get("memory"),
                "unread_notes": state["notes"].get("unread", 0),
                "llm_calls_5min": state["llm"].get("total", 0),
            },
        }
