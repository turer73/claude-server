"""Durable Event Dispatcher — server.db.events kuyruğunu tail edip AgentBus'a verir.

Rol: kayıp-event köprüsü. emit_event() zaten DB'ye yazar; ama çok-worker/thread/restart
yüzünden leader'ın bus'ı her event'i duyamayabilir. Dispatcher leader-worker'da çalışır,
events tablosunu kalıcı cursor ile tarar ve yerel bus'a publish eder.

Loop-guard: dispatcher'dan çıkan event'lere from_db=True işareti konur;
event_spine_bridge bu event'leri DB'ye GERİ yazmaz (sonsuz döngü önlenir).
Cursor kalıcıdır (event_dispatch_cursor) — restart sonrası kaldığı yerden devam eder.

Not: tek dispatcher çalışmalı (leader). İki dispatcher aynı event'i iki bus'a verirse
bridge'ler ikişer yazar → duplicate. main.py bağlamayı leader-kontrolü ile yapar.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core.agent_bus import Event
from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1.0
_BATCH = 200

_SCHEMA = "CREATE TABLE IF NOT EXISTS event_dispatch_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), cursor INTEGER NOT NULL DEFAULT 0);"
_schema_ready = False


class DurableEventDispatcher:
    def __init__(self, bus: Any) -> None:
        self.bus = bus
        self.cursor = 0
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def _conn(self):
        global _schema_ready
        conn = get_conn(server_db_path())
        if not _schema_ready:
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
                _schema_ready = True
            except Exception as e:
                logger.warning("event_dispatch_cursor ensure failed: %s", e)
        return conn

    def _load_cursor(self) -> int:
        try:
            conn = self._conn()
            try:
                row = conn.execute("SELECT cursor FROM event_dispatch_cursor WHERE id=1").fetchone()
            finally:
                conn.close()
            return int(row["cursor"]) if row else 0
        except Exception as e:
            logger.warning("dispatcher cursor load failed: %s", e)
            return 0

    def _save_cursor(self, cursor: int) -> None:
        try:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO event_dispatch_cursor (id, cursor) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor",
                    (cursor,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("dispatcher cursor save failed: %s", e)

    async def start(self) -> None:
        self.cursor = self._load_cursor()
        self._task = asyncio.create_task(self._run())
        logger.info("durable event dispatcher started (cursor=%s)", self.cursor)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("dispatcher poll failed: %s", e)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll(self) -> None:
        rows = await asyncio.to_thread(self._fetch_rows)
        for r in rows:
            payload: dict[str, Any] = {}
            raw = r["payload"]
            if isinstance(raw, str) and raw:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    payload = {"_raw": raw[:200]}
            event = Event(type=r["type"], source=r["source"] or "", payload=payload)
            event.id = r["id"]
            event.from_db = True
            try:
                await self.bus.publish(event)
            except Exception as e:
                logger.warning("dispatcher bus publish failed (id=%s): %s", r["id"], e)
            self.cursor = r["id"]
        if rows:
            self._save_cursor(self.cursor)

    def _fetch_rows(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, type, source, payload FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (self.cursor, _BATCH),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def create_dispatcher(bus: Any) -> DurableEventDispatcher:
    return DurableEventDispatcher(bus)
