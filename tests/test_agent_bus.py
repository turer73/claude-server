"""Tests for AgentBus — async pub/sub inter-agent communication."""

from __future__ import annotations

import pytest

from app.core.agent_bus import AgentBus, Event, get_bus


@pytest.fixture
def bus():
    return AgentBus()


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
