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
from datetime import UTC, datetime
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
_MIN_OBSERVATIONS = 10
_LEARN_COOLDOWN = 3600
# score_after ölçüm-gecikmesi: eşik-ayarının etkisini görmek için ayar-sonrası bu kadar bekleyip
# o anki 15min-ortalamayı geriye yazıyoruz (score_before ile aynı pencere — karşılaştırılabilir).
# Bağımsız asyncio-task ile zamanlanır (_run_loop'un self._interval tick-cadence'ına BAĞLI DEĞİL —
# prod'da interval=3600s, tick'e bağlı olsaydı 15dk hedefi saatlik-tick'e kayardı, code-review#307-P2).
_SCORE_AFTER_DELAY = 900
_SCORE_AFTER_RETRY_DELAY = 60
# restart-recovery gec-kalma toleransi (code-review#307-P2 2.tur): bundan daha eski satirlar
# geri-yuklenmez — aksi halde upgrade-oncesi biriken gunler/haftalar-eski score_after=NULL
# satirlari "bugunun" ortalamasiyla kirletirdik (production'da fix-oncesi 68 boyle satir vardi).
_SCORE_AFTER_STALE_CUTOFF = 3600


def _ensure_learning_table() -> None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.executescript(_LEARNING_SCHEMA)
        con.commit()
        con.close()
    except sqlite3.Error as e:
        log.warning("learning schema error: %s", e)


def _record_learning_event(event_type: str, detail: str, score_before: float | None = None, score_after: float | None = None) -> int | None:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        cur = con.execute(
            "INSERT INTO learning_events (event_type, detail, score_before, score_after) VALUES (?, ?, ?, ?)",
            (event_type, detail, score_before, score_after),
        )
        con.commit()
        row_id = cur.lastrowid
        con.close()
        return row_id
    except sqlite3.Error as e:
        log.warning("learning event record error: %s", e)
        return None


def _update_score_after(event_id: int, score_after: float) -> bool:
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.execute("UPDATE learning_events SET score_after=? WHERE id=?", (score_after, event_id))
        con.commit()
        con.close()
        return True
    except sqlite3.Error as e:
        log.warning("score_after update error: %s", e)
        return False


def _load_pending_score_after() -> dict[int, float]:
    """Restart-recovery (code-review#307-P2): score_after hâlâ NULL olan threshold_adjustment
    satırlarını created_at'tan due-ts hesaplayarak geri-yükler — süreç yeniden-başlarsa
    in-memory pending-state kaybolup ölçüm sonsuza dek NULL kalmasın. _SCORE_AFTER_STALE_CUTOFF'tan
    daha eski satırlar ATLANIR (code-review#307-P2 2.tur): aksi halde bu fix-öncesi biriken
    günler-eski satırlar "bugünün" ortalamasıyla üzerine yazılıp tarihsel veri kirlenirdi."""
    pending: dict[int, float] = {}
    now = time.time()
    try:
        con = sqlite3.connect(_MEMORY_DB, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, created_at FROM learning_events WHERE event_type='threshold_adjustment' AND score_after IS NULL"
        ).fetchall()
        con.close()
        for row in rows:
            try:
                created_ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
            except (TypeError, ValueError):
                continue
            due_ts = created_ts + _SCORE_AFTER_DELAY
            if now - due_ts > _SCORE_AFTER_STALE_CUTOFF:
                continue  # cok-eski, dogru pencereyi artik olcemeyiz - uydurma-veri yazma, NULL kalsin
            pending[row["id"]] = due_ts
    except sqlite3.Error as e:
        log.warning("pending score_after load error: %s", e)
    return pending


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
        self._last_run: str | None = None
        # event_id -> due-ts: her learn-event'in "etki oldu mu" ölçümü ayardan _SCORE_AFTER_DELAY
        # sonra yapılır (flywheel'in açık-döngüsü: score_after NULL kalıp bir daha doldurulmuyordu).
        self._pending_score_after: dict[int, float] = {}
        self._score_after_tasks: set[asyncio.Task[None]] = set()

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
            # last_run sözleşmesi ISO-string (agent_system/schemas); epoch-float dashboard'da
            # new Date(ms) ile 1970'e çözülüyor (disc: "20623g önce"). Skor-ts'i değil döngü-tick'i yansıt.
            "last_run": self._last_run,
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
        try:
            from app.core.presence_manager import presence

            presence.upsert("learning_loop", "leader-1", "learning", "klipper", "klipper", {"improvement": True})
            presence.heartbeat("learning_loop", status="idle")
        except Exception as e:
            log.warning("presence register failed (learning_loop): %s", e)
        bus = get_bus()
        bus.register_agent("learning_loop", "Closed-loop improvement engine")
        bus.subscribe("critic:score", self._on_score)
        _ensure_learning_table()
        self._pending_score_after = _load_pending_score_after()
        for event_id, due_ts in self._pending_score_after.items():
            self._spawn_score_after_task(event_id, due_ts)
        log.info(
            "learning loop started (interval=%ss, %s pending score_after recovered)",
            self._interval,
            len(self._pending_score_after),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        score_after_tasks = list(self._score_after_tasks)
        for t in score_after_tasks:
            t.cancel()
        if score_after_tasks:
            await asyncio.gather(*score_after_tasks, return_exceptions=True)
        self._score_after_tasks.clear()
        bus = get_bus()
        bus.unsubscribe("critic:score", self._on_score)
        log.info("learning loop stopped")

    def _spawn_score_after_task(self, event_id: int, due_ts: float) -> None:
        task = asyncio.create_task(self._resolve_score_after(event_id, due_ts))
        self._score_after_tasks.add(task)
        task.add_done_callback(self._score_after_tasks.discard)

    async def _resolve_score_after(self, event_id: int, due_ts: float) -> None:
        """due_ts'e kadar bekleyip score_after'ı doldurur — _run_loop'un tick-cadence'ından
        BAĞIMSIZ (code-review#307-P2: prod interval=3600s iken tick'e bağlı olsaydı 15dk hedefi
        saatlik-tick'e kayardı). Geçici DB-hatasında/veri-yokluğunda SINIRSIZ retry eder — sabit
        deneme-sayısında pes etmek yalnız restart-recovery'ye güveniyordu, aynı süreç çalışırken
        bir daha hiç denenmiyordu (code-review#307-P2 2.tur). stop() bu task'i iptal eder, sonsuz
        döngü güvenli — event'ler nadir (cooldown=1h) olduğundan kaynak-maliyeti yok."""
        await asyncio.sleep(max(0.0, due_ts - time.time()))
        attempt = 0
        while True:
            avg_15min = self._get_windows().get("15min")
            if avg_15min is not None:
                ok = await asyncio.to_thread(_update_score_after, event_id, avg_15min)
                if ok:
                    self._pending_score_after.pop(event_id, None)
                    return
            attempt += 1
            if attempt % 10 == 0:
                log.warning("score_after event #%s hala cozulemedi (%s deneme)", event_id, attempt)
            await asyncio.sleep(_SCORE_AFTER_RETRY_DELAY)

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
                self._last_run = datetime.now(UTC).isoformat()
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

            event_id = await asyncio.to_thread(
                _record_learning_event,
                "threshold_adjustment",
                detail,
                score_before=avg_15min,
            )
            if event_id is not None:
                due_ts = now + _SCORE_AFTER_DELAY
                self._pending_score_after[event_id] = due_ts
                self._spawn_score_after_task(event_id, due_ts)

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
