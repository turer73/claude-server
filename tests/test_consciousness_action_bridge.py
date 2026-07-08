"""Tests for Consciousness→Action Bridge (Faz 3)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def mock_consciousness():
    """ConsciousnessStream mock."""
    from app.core.consciousness import ConsciousnessStream

    stream = MagicMock(spec=ConsciousnessStream)
    stream._interval = 15
    stream._running = True
    stream._thought_count = 0
    stream._recent_thoughts = []
    stream._llm_timer = 0
    stream._prev_emotion = None
    stream._last_thought = None
    stream._devops_agent = None
    stream._last_concern_emit = {}
    stream._concern_cooldown = 1800
    return stream


@pytest.fixture(autouse=True)
def mock_time_monotonic():
    """time.monotonic() her test için sabit değer döndürsün."""
    with patch("time.monotonic", return_value=1000000.0):
        yield


@pytest.mark.asyncio
async def test_concerned_emotion_emits_event(mock_consciousness) -> None:
    """'concerned' emotion → event emit edilir."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "cron:fail",
        "emotion": "concerned",
        "content": "3 cron fail son 24h",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args.kwargs["type"] == "consciousness:concerned:cron"
        assert call_args.kwargs["source"] == "consciousness"
        assert call_args.kwargs["severity"] == "warn"


@pytest.mark.asyncio
async def test_calm_emotion_no_event(mock_consciousness) -> None:
    """'calm' emotion → event emit edilmez."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "idle",
        "emotion": "calm",
        "content": "her sey sakin",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.events.emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_busy_emotion_no_event(mock_consciousness) -> None:
    """'busy' emotion → event emit edilmez (sadece concerned)."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "metric:cpu",
        "emotion": "busy",
        "content": "CPU %85",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_alert_critical_focus_skipped(mock_consciousness) -> None:
    """alert:critical focus → event emit edilmez (zaten alert var)."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "alert:critical",
        "emotion": "concerned",
        "content": "2 kritik uyari",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_poison_focus_emits_spawn_event(mock_consciousness) -> None:
    """spawn:poison focus → consciousness:concerned:spawn event."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "spawn:poison",
        "emotion": "concerned",
        "content": "2 spawn poison DLQ'da",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args.kwargs["type"] == "consciousness:concerned:spawn"


@pytest.mark.asyncio
async def test_unknown_focus_emits_general_event(mock_consciousness) -> None:
    """Bilinmeyen focus → consciousness:concerned:general event."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "unknown:focus",
        "emotion": "concerned",
        "content": "Bilinmeyen sorun",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event") as mock_emit:
        await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args.kwargs["type"] == "consciousness:concerned:general"


@pytest.mark.asyncio
async def test_emit_failure_does_not_crash(mock_consciousness) -> None:
    """Event emit hatası → thought akışı bozulmaz (fail-safe)."""
    from app.core.consciousness import ConsciousnessStream

    thought = {
        "timestamp": "2026-07-08T12:00:00",
        "focus": "cron:fail",
        "emotion": "concerned",
        "content": "Cron fail",
        "source_data": "{}",
        "is_deep": 0,
    }

    with patch("app.core.consciousness._emit_event", side_effect=Exception("DB error")):
        with patch("app.core.consciousness.log") as mock_log:
            await ConsciousnessStream._maybe_emit_concern_event(mock_consciousness, thought)
            mock_log.warning.assert_called_once()
            assert "emit failed" in mock_log.warning.call_args[0][0]
