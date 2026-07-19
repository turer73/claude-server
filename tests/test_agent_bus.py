"""Tests for AgentBus — async pub/sub inter-agent communication."""

from __future__ import annotations

import asyncio

import pytest

from app.core.agent_bus import AgentBus, Event, get_bus


@pytest.fixture
async def bus():
    instance = AgentBus()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
def sample_event():
    return Event(type="test:event", source="test_agent", payload={"key": "value"})


class TestEvent:
    def test_event_creation(self):
        e = Event(type="thought:new", source="critic", payload={"score": 7})
        assert e.type == "thought:new"
        assert e.source == "critic"
        assert e.payload == {"score": 7}
        assert e.id > 0
        assert e.timestamp > 0

    def test_event_auto_increment_id(self):
        e1 = Event(type="a", source="s1", payload={})
        e2 = Event(type="b", source="s2", payload={})
        assert e2.id > e1.id


class TestAgentBusCore:
    async def test_register_agent(self, bus):
        bus.register_agent("test_agent", "A test agent")
        status = bus.get_status()
        assert len(status["agents"]) == 1
        assert status["agents"][0]["name"] == "test_agent"
        assert status["agents"][0]["description"] == "A test agent"

    async def test_register_multiple_agents(self, bus):
        bus.register_agent("agent_a")
        bus.register_agent("agent_b")
        assert len(bus.get_status()["agents"]) == 2

    async def test_subscribe_and_publish(self, bus):
        bus.register_agent("subscriber")
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe("test:event", handler)
        await bus.publish(Event(type="test:event", source="publisher", payload={}))
        assert len(received) == 1
        assert received[0].type == "test:event"

    async def test_subscribe_to_all(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event.type)

        bus.subscribe_to_all(handler)
        await bus.publish(Event(type="type_a", source="s", payload={}))
        await bus.publish(Event(type="type_b", source="s", payload={}))
        assert received == ["type_a", "type_b"]

    async def test_publish_no_subscribers(self, bus):
        bus.register_agent("lonely")
        await bus.publish(Event(type="orphan:event", source="lonely", payload={}))
        assert len(bus.recent_events(limit=10)) == 1

    async def test_duplicate_thought_id_is_dispatched_once_across_recovery_types(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_to_all(handler)
        await bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 42, "thought": {}}))
        await bus.publish(Event(type="thought:deep", source="critic:loop", payload={"thought_id": 42, "thought": {}}))

        assert len(received) == 1
        assert received[0].source == "consciousness"
        assert len(bus.recent_events(limit=10)) == 1
        assert bus.get_status()["thought_dedupe_size"] == 1

    async def test_duplicate_thought_retries_only_failed_handlers(self, bus):
        successful_calls = 0
        retry_calls = 0
        retried = asyncio.Event()
        bus._thought_retry_base_delay = 0

        async def successful_handler(event: Event):
            nonlocal successful_calls
            successful_calls += 1

        async def retry_handler(event: Event):
            nonlocal retry_calls
            retry_calls += 1
            if retry_calls == 1:
                raise RuntimeError("transient failure")
            retried.set()

        bus.subscribe_to_all(successful_handler)
        bus.subscribe_to_all(retry_handler)

        await bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 43}))
        await asyncio.wait_for(retried.wait(), timeout=1)

        assert successful_calls == 1
        assert retry_calls == 2
        assert len(bus.recent_events(limit=10)) == 1
        assert bus.get_status()["thought_retry_pending"] == 0

    async def test_concurrent_recovery_waits_then_retries_failed_handler(self, bus):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        bus._thought_retry_base_delay = 1

        async def handler(event: Event):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("first publication failed")

        bus.subscribe_to_all(handler)
        direct = asyncio.create_task(bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 44})))
        await entered.wait()
        recovery = asyncio.create_task(bus.publish(Event(type="thought:deep", source="critic:loop", payload={"thought_id": 44})))
        release.set()
        await asyncio.gather(direct, recovery)

        assert calls == 2
        assert len(bus.recent_events(limit=10)) == 1

    async def test_stop_cancels_pending_thought_retries(self, bus):
        bus._thought_retry_base_delay = 60

        async def failing_handler(event: Event):
            raise RuntimeError("persistent failure")

        bus.subscribe_to_all(failing_handler)
        await bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 46}))

        assert bus.get_status()["thought_retry_pending"] == 1
        assert len(bus._thought_retry_tasks) == 1

        await bus.stop()

        assert bus.get_status()["thought_retry_pending"] == 0
        assert bus._thought_retry_tasks == {}

    async def test_persistent_thought_failure_exhausts_bounded_retries(self, bus):
        bus._thought_retry_base_delay = 0
        bus._thought_retry_limit = 2
        exhausted = asyncio.Event()
        calls = 0

        async def failing_handler(event: Event):
            nonlocal calls
            calls += 1
            if calls == 3:  # initial delivery + two retry attempts
                exhausted.set()
            raise RuntimeError("persistent failure")

        bus.subscribe_to_all(failing_handler)
        await bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 47}))
        await asyncio.wait_for(exhausted.wait(), timeout=1)
        while bus._thought_retry_tasks:
            await asyncio.sleep(0)

        assert calls == 3
        assert bus.get_status()["thought_retry_pending"] == 0
        assert bus.get_status()["thought_retry_exhausted"] == 1

    async def test_recursive_publication_of_same_thought_does_not_deadlock(self, bus):
        calls = 0

        async def handler(event: Event):
            nonlocal calls
            calls += 1
            await bus.publish(Event(type="thought:deep", source="handler", payload={"thought_id": 45}))

        bus.subscribe_to_all(handler)

        await asyncio.wait_for(
            bus.publish(Event(type="thought:new", source="consciousness", payload={"thought_id": 45})),
            timeout=1,
        )

        assert calls == 1

    async def test_unsubscribe(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe("test:event", handler)
        bus.unsubscribe("test:event", handler)
        await bus.publish(Event(type="test:event", source="s", payload={}))
        assert len(received) == 0

    async def test_unsubscribe_not_subscribed(self, bus):
        async def handler(event: Event):
            pass

        bus.unsubscribe("nonexistent", handler)
        bus.unsubscribe("test:event", handler)

    async def test_handler_failure_logged(self, bus, caplog):
        async def failing_handler(event: Event):
            raise ValueError("handler failed")

        bus.subscribe("test:event", failing_handler)
        await bus.publish(Event(type="test:event", source="s", payload={}))
        assert "handler failed" in caplog.text

    async def test_publish_counts_events(self, bus):
        bus.register_agent("counter")

        async def handler(event: Event):
            pass

        bus.subscribe("test:event", handler)
        await bus.publish(Event(type="test:event", source="counter", payload={}))
        agent_info = bus._agents["counter"]
        assert agent_info["events_published"] == 1

    async def test_event_log_max_size(self, bus):
        bus._max_log = 3
        for i in range(5):
            await bus.publish(Event(type=f"e{i}", source="s", payload={}))
        assert len(bus._event_log) == 3
        assert bus._event_log[0].type == "e2"

    async def test_agent_status_update(self, bus):
        bus.register_agent("dynamic")
        bus.agent_status("dynamic", custom_field="hello")
        assert bus._agents["dynamic"]["custom_field"] == "hello"

    async def test_get_status_structure(self, bus):
        bus.register_agent("agent_a")
        bus.register_agent("agent_b")

        async def h1(event):
            pass

        async def h2(event):
            pass

        bus.subscribe("type1", h1)
        bus.subscribe("type2", h2)

        status = bus.get_status()
        assert "agents" in status
        assert "subscriber_count" in status
        assert "event_log_size" in status
        assert "event_types" in status
        assert status["subscriber_count"] == 2
        assert len(status["agents"]) == 2

    async def test_recent_events(self, bus):
        for i in range(3):
            await bus.publish(Event(type=f"e{i}", source="s", payload={"i": i}))
        recent = bus.recent_events(limit=2)
        assert len(recent) == 2
        assert recent[1]["type"] == "e2"

    async def test_handler_received_count(self, bus):
        bus.register_agent("receiver")

        async def handler(event: Event):
            pass

        handler.__qualname__ = "receiver.handler"
        bus.subscribe("test:event", handler)
        await bus.publish(Event(type="test:event", source="sender", payload={}))
        assert bus._agents["receiver"]["events_received"] == 1


class TestGetBus:
    def test_singleton(self):
        b1 = get_bus()
        b2 = get_bus()
        assert b1 is b2

    def test_clear_and_recreate(self):
        import app.core.agent_bus as bus_module

        bus_module._bus = None
        b = get_bus()
        assert isinstance(b, AgentBus)
