"""Tests for AgentBus ↔ events spine bridge."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.agent_bus import AgentBus, Event
from app.core.event_spine_bridge import _bus_event_to_spine, bridge_handler


@pytest.fixture
def bus():
    b = AgentBus()
    b.register_agent("test_agent")
    return b


@pytest.fixture
def sample_event():
    return Event(type="thought:new", source="consciousness", payload={"focus": "cron:fail", "emotion": "concerned", "content": "test"})


class TestSpineDirection:
    """Yön 1: events spine → AgentBus (emit_event → bus publish)."""

    def test_emit_event_publishes_to_bus(self, bus):
        with patch("app.core.events._bus_publish") as mock_publish:
            from app.core.events import emit_event

            emit_event(type="anomaly", source="test", title="Test", severity="warn", payload={"val": 1})
            mock_publish.assert_called_once()
            args = mock_publish.call_args[0]
            assert args[0] == "anomaly"
            assert args[1] == "test"

    def test_bus_publish_loop_guard_skips_bridge_source(self, bus):
        """bridge: ile başlayan source bus'a publish edilmez (loop guard)."""
        with patch("app.core.agent_bus.AgentBus.publish") as mock_bus_pub:
            from app.core.events import _bus_publish

            _bus_publish("test", "bridge:spine", "title", "info", {})
            mock_bus_pub.assert_not_called()

    def test_bus_publish_normal_source(self, bus):
        """Normal source bus'a publish edilir."""
        from app.core.events import _bus_publish

        with patch("app.core.agent_bus.AgentBus.publish") as mock_bus_pub:
            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.is_running.return_value = True
                _bus_publish("anomaly", "cron:test", "Test Event", "warn", {"val": 1})
                mock_bus_pub.assert_called_once()
                ev = mock_bus_pub.call_args[0][0]
                assert ev.type == "spine:anomaly"
                assert ev.source == "bridge:spine"
                assert ev.payload["source"] == "cron:test"


class TestBusDirection:
    """Yön 2: AgentBus → events spine (bridge_handler)."""

    def test_loop_guard_skips_bridge_source(self, bus, sample_event):
        """bridge: kaynaklı event spine'a yazılmaz (loop guard)."""
        ev = Event(type="spine:anomaly", source="bridge:spine", payload={})
        with patch("app.core.events.emit_event") as mock_emit:
            _bus_event_to_spine(ev)
            mock_emit.assert_not_called()

    async def test_thought_new_written_to_spine(self, bus, sample_event):
        """thought:new event spine'a emit_event ile yazılır."""
        with patch("app.core.events.emit_event") as mock_emit:
            _bus_event_to_spine(sample_event)
            mock_emit.assert_called_once()
            args = mock_emit.call_args[1]
            assert args["type"] == "agentbus:thought"
            assert args["source"] == "bridge:agentbus"

    async def test_unknown_event_type(self, bus):
        """Bilinmeyen event type spine: prefix ile gider."""
        ev = Event(type="unknown:type", source="test", payload={"msg": "hello"})
        with patch("app.core.events.emit_event") as mock_emit:
            _bus_event_to_spine(ev)
            mock_emit.assert_called_once()
            args = mock_emit.call_args[1]
            assert args["type"] == "agentbus:unknown:type"

    async def test_bridge_handler_wired(self, bus):
        """bridge_handler subscribe_to_all ile çalışır."""
        handler_called = False

        async def capture(event):
            nonlocal handler_called
            handler_called = True

        bus.subscribe_to_all(capture)
        await bus.publish(Event(type="test:event", source="test_agent", payload={}))
        assert handler_called

    async def test_bridge_integration(self, bus):
        """Gerçek akış: bus publish → bridge → emit_event."""
        bus.subscribe_to_all(bridge_handler)
        ev = Event(type="critic:score", source="critic", payload={"score": 4.5, "thought_id": 1})
        with patch("app.core.events.emit_event") as mock_emit:
            await bus.publish(ev)
            mock_emit.assert_called_once()
            args = mock_emit.call_args[1]
            assert args["type"] == "agentbus:critic"

    async def test_bridge_loop_integration(self, bus):
        """Loop guard test: spine→bus→bridge→spine sonsuz döngü olmaz."""
        bus.subscribe_to_all(bridge_handler)
        ev = Event(type="spine:anomaly", source="bridge:spine", payload={})
        with patch("app.core.events.emit_event") as mock_emit:
            await bus.publish(ev)
            # bridge_handler çağrılır, _bus_event_to_spine source bridge: olduğu için emit_event çağrılmaz
            mock_emit.assert_not_called()


class TestBridgeEdgeCases:
    async def test_empty_payload(self, bus):
        """Boş payload çökertmez."""
        ev = Event(type="thought:new", source="consciousness", payload={})
        with patch("app.core.events.emit_event") as mock_emit:
            _bus_event_to_spine(ev)
            mock_emit.assert_called_once()

    async def test_long_content_truncated(self, bus):
        """Uzun payload değerleri kesilir."""
        ev = Event(type="critic:score", source="critic", payload={"detail": "x" * 500})
        with patch("app.core.events.emit_event") as mock_emit:
            _bus_event_to_spine(ev)
            mock_emit.assert_called_once()
            args = mock_emit.call_args[1]
            assert "..." in str(args.get("detail", ""))

    async def test_bridge_does_not_crash_on_exception(self, bus):
        """Handler exception yutulur, diğer event'ler etkilenmez."""
        events_received = []

        async def failing_handler(event):
            raise RuntimeError("fail")

        async def good_handler(event):
            events_received.append(event.type)

        bus.subscribe_to_all(failing_handler)
        bus.subscribe_to_all(good_handler)
        await bus.publish(Event(type="test:ok", source="test", payload={}))
        assert len(events_received) == 1
