"""Agent Bus — lightweight async pub/sub for inter-agent communication.

Each agent registers itself, publishes events by type (e.g. 'thought:new',
'critic:score'), and subscribes to event types it cares about.
The bus dispatches events asynchronously — subscribers are awaited
concurrently and failures never block the publisher.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any

log = logging.getLogger("agent_bus")

EventHandler = Callable[["Event"], Coroutine[None, None, None]]
_active_thought_keys: ContextVar[frozenset[tuple[int, int]]] = ContextVar("agent_bus_active_thought_keys", default=frozenset())


@dataclass
class Event:
    type: str
    source: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    id: int = 0
    from_db: bool = field(default=False)

    _next_id: int = 0

    def __post_init__(self) -> None:
        Event._next_id += 1
        self.id = Event._next_id


class AgentBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._event_log: list[Event] = []
        self._max_log = 500
        self._thought_handler_acks: dict[int, set[EventHandler]] = {}
        self._thought_locks: dict[int, asyncio.Lock] = {}
        self._thought_publishers: dict[int, int] = {}
        self._thought_retry_deliveries: dict[int, dict[EventHandler, Event]] = {}
        self._thought_retry_tasks: dict[int, asyncio.Task[None]] = {}
        self._logged_thought_ids: set[int] = set()
        self._thought_order: deque[int] = deque()
        self._max_seen_thoughts = 1000
        self._thought_retry_limit = 5
        self._thought_retry_base_delay = 0.1
        self._thought_retry_exhausted = 0
        self._stopping = False

    def register_agent(self, name: str, description: str = "") -> None:
        self._agents[name] = {
            "name": name,
            "description": description,
            "registered_at": time.time(),
            "events_published": 0,
            "events_received": 0,
        }
        log.info("agent registered: %s", name)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._subscribers.get(event_type)
        if handlers:
            try:
                handlers.remove(handler)
            except ValueError:
                pass

    def subscribe_to_all(self, handler: EventHandler) -> None:
        self._subscribers.setdefault("*", []).append(handler)

    def agent_status(self, name: str, **extra: Any) -> None:
        if name in self._agents:
            self._agents[name].update(extra)

    def agent_names(self) -> list[str]:
        """Registered agent adlari (presence heartbeat icin)."""
        return list(self._agents.keys())

    async def publish(self, event: Event) -> None:
        thought_id = self._thought_id(event)
        if thought_id is None:
            await self._publish_event(event)
            return

        lock = self._thought_locks.get(thought_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thought_locks[thought_id] = lock
            self._thought_handler_acks[thought_id] = set()
            self._thought_publishers[thought_id] = 0
            self._thought_order.append(thought_id)

        thought_key = (id(self), thought_id)
        if thought_key in _active_thought_keys.get():
            log.debug("recursive thought event suppressed: type=%s thought_id=%s", event.type, thought_id)
            return

        # Direct publication and the polling recovery path can race.  Serialising
        # one thought lets the recovery publication retry only handlers that
        # failed, without calling handlers that already acknowledged it again.
        self._thought_publishers[thought_id] += 1
        try:
            async with lock:
                token = _active_thought_keys.set(_active_thought_keys.get() | {thought_key})
                try:
                    await self._publish_thought_event(event, thought_id)
                finally:
                    _active_thought_keys.reset(token)
        finally:
            self._thought_publishers[thought_id] -= 1
            self._trim_thought_state()

    async def _publish_event(self, event: Event) -> None:
        self._record_event(event)
        handlers = self._handlers_for(event)
        if handlers:
            await asyncio.gather(*(self._safe_dispatch(handler, event) for handler in handlers))

    async def _publish_thought_event(self, event: Event, thought_id: int) -> None:
        first_publication = thought_id not in self._logged_thought_ids
        if first_publication:
            self._logged_thought_ids.add(thought_id)
            self._record_event(event)

        acknowledged = self._thought_handler_acks[thought_id]
        handlers = [handler for handler in self._handlers_for(event) if handler not in acknowledged]
        if not handlers:
            if not first_publication:
                log.debug("duplicate thought event suppressed: type=%s thought_id=%s", event.type, thought_id)
            if self._thought_retry_deliveries.get(thought_id):
                self._ensure_thought_retry(thought_id)
            return

        results = await asyncio.gather(*(self._safe_dispatch(handler, event) for handler in handlers))
        retry_deliveries = self._thought_retry_deliveries.setdefault(thought_id, {})
        for handler, succeeded in zip(handlers, results, strict=True):
            if succeeded:
                acknowledged.add(handler)
                retry_deliveries.pop(handler, None)
            else:
                retry_deliveries[handler] = event
        if retry_deliveries:
            self._ensure_thought_retry(thought_id)
        else:
            self._thought_retry_deliveries.pop(thought_id, None)

    def _ensure_thought_retry(self, thought_id: int) -> None:
        if self._stopping:
            return
        task = self._thought_retry_tasks.get(thought_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._retry_thought_handlers(thought_id))
        self._thought_retry_tasks[thought_id] = task
        task.add_done_callback(partial(self._thought_retry_finished, thought_id))

    async def _retry_thought_handlers(self, thought_id: int) -> None:
        current_task = asyncio.current_task()
        try:
            for attempt in range(1, self._thought_retry_limit + 1):
                delay = min(self._thought_retry_base_delay * (2 ** (attempt - 1)), 1.0)
                await asyncio.sleep(delay)
                lock = self._thought_locks.get(thought_id)
                if lock is None:
                    return
                async with lock:
                    deliveries = self._thought_retry_deliveries.get(thought_id)
                    if not deliveries:
                        return
                    acknowledged = self._thought_handler_acks.get(thought_id)
                    if acknowledged is None:
                        return

                    for handler, event in list(deliveries.items()):
                        if handler not in self._handlers_for(event):
                            deliveries.pop(handler, None)
                            continue
                        thought_key = (id(self), thought_id)
                        token = _active_thought_keys.set(_active_thought_keys.get() | {thought_key})
                        try:
                            succeeded = await self._safe_dispatch(handler, event)
                        finally:
                            _active_thought_keys.reset(token)
                        if succeeded:
                            acknowledged.add(handler)
                            deliveries.pop(handler, None)

                    if not deliveries:
                        self._thought_retry_deliveries.pop(thought_id, None)
                        return
                    if attempt == self._thought_retry_limit:
                        failed_handlers = len(deliveries)
                        self._thought_retry_deliveries.pop(thought_id, None)
                        self._thought_retry_exhausted += failed_handlers
                        log.error(
                            "thought delivery retries exhausted: thought_id=%s failed_handlers=%s",
                            thought_id,
                            failed_handlers,
                        )
                        return
        finally:
            if self._thought_retry_tasks.get(thought_id) is current_task:
                self._thought_retry_tasks.pop(thought_id, None)

    def _thought_retry_finished(self, thought_id: int, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("thought delivery retry task failed")
            if self._thought_retry_deliveries.get(thought_id):
                self._ensure_thought_retry(thought_id)

    async def stop(self) -> None:
        """Cancel and observe every pending delivery retry."""
        self._stopping = True
        tasks = list(self._thought_retry_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._thought_retry_tasks.clear()
        self._thought_retry_deliveries.clear()

    def _record_event(self, event: Event) -> None:
        self._event_log.append(event)
        if len(self._event_log) > self._max_log:
            self._event_log.pop(0)

        if event.source in self._agents:
            self._agents[event.source]["events_published"] += 1

    def _handlers_for(self, event: Event) -> list[EventHandler]:
        handlers: list[EventHandler] = []
        handlers.extend(self._subscribers.get(event.type, []))
        handlers.extend(self._subscribers.get("*", []))
        return handlers

    @staticmethod
    def _thought_id(event: Event) -> int | None:
        if event.type not in {"thought:new", "thought:deep"}:
            return None
        try:
            thought_id = int(event.payload.get("thought_id", 0))
        except (TypeError, ValueError):
            return None
        if thought_id <= 0:
            return None
        return thought_id

    def _trim_thought_state(self) -> None:
        """Bound acknowledgement memory without evicting an active dispatch."""
        rotations = 0
        while len(self._thought_order) > self._max_seen_thoughts and rotations < len(self._thought_order):
            expired = self._thought_order.popleft()
            lock = self._thought_locks.get(expired)
            retry_task = self._thought_retry_tasks.get(expired)
            if (
                self._thought_publishers.get(expired, 0)
                or (lock is not None and lock.locked())
                or (retry_task is not None and not retry_task.done())
            ):
                self._thought_order.append(expired)
                rotations += 1
                continue
            self._thought_locks.pop(expired, None)
            self._thought_publishers.pop(expired, None)
            self._thought_handler_acks.pop(expired, None)
            self._thought_retry_deliveries.pop(expired, None)
            self._logged_thought_ids.discard(expired)
            rotations = 0

    async def _safe_dispatch(self, handler: EventHandler, event: Event) -> bool:
        try:
            await handler(event)
            for name, info in self._agents.items():
                if handler.__qualname__.startswith(f"{name}."):
                    info["events_received"] = info.get("events_received", 0) + 1
            return True
        except Exception as e:
            log.warning("handler %s failed on %s: %s", handler.__name__, event.type, e)
            return False

    def get_status(self) -> dict[str, Any]:
        return {
            "agents": list(self._agents.values()),
            "subscriber_count": sum(len(h) for h in self._subscribers.values()),
            "event_log_size": len(self._event_log),
            "event_types": list(self._subscribers.keys()),
            "thought_dedupe_size": len(self._thought_handler_acks),
            "thought_retry_pending": sum(len(deliveries) for deliveries in self._thought_retry_deliveries.values()),
            "thought_retry_exhausted": self._thought_retry_exhausted,
        }

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"id": e.id, "type": e.type, "source": e.source, "ts": e.timestamp, "payload_keys": list(e.payload.keys())}
            for e in self._event_log[-limit:]
        ]


_bus: AgentBus | None = None


def get_bus() -> AgentBus:
    global _bus
    if _bus is None:
        _bus = AgentBus()
    return _bus
