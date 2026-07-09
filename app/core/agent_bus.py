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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("agent_bus")

EventHandler = Callable[["Event"], Coroutine[None, None, None]]


@dataclass
class Event:
    type: str
    source: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    id: int = 0

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

    async def publish(self, event: Event) -> None:
        self._event_log.append(event)
        if len(self._event_log) > self._max_log:
            self._event_log.pop(0)

        if event.source in self._agents:
            self._agents[event.source]["events_published"] += 1

        handlers: list[EventHandler] = []
        handlers.extend(self._subscribers.get(event.type, []))
        handlers.extend(self._subscribers.get("*", []))

        if not handlers:
            return

        tasks = []
        for handler in handlers:
            tasks.append(self._safe_dispatch(handler, event))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_dispatch(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
            for name, info in self._agents.items():
                if handler.__qualname__.startswith(f"{name}."):
                    info["events_received"] = info.get("events_received", 0) + 1
        except Exception as e:
            log.warning("handler %s failed on %s: %s", handler.__name__, event.type, e)

    def get_status(self) -> dict[str, Any]:
        return {
            "agents": list(self._agents.values()),
            "subscriber_count": sum(len(h) for h in self._subscribers.values()),
            "event_log_size": len(self._event_log),
            "event_types": list(self._subscribers.keys()),
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
