"""Tests for WebSocket monitor handler."""

import pytest
from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient

from tests.conftest import TEST_API_KEY


def test_ws_monitor_sends_metrics(app, tmp_path, monkeypatch):
    """Test monitor WebSocket sends metrics and handles disconnect."""
    mock_metrics = {
        "cpu_percent": 45.0,
        "memory_percent": 60.0,
        "disk_percent": 34.0,
        "temperature": 55.0,
    }

    mock_monitor = MagicMock()
    mock_monitor.collect_metrics.return_value = mock_metrics

    with patch("app.ws.monitor._monitor", mock_monitor):
        client = TestClient(app)
        with client.websocket_connect("/ws/monitor") as ws:
            data = ws.receive_json()
            assert "cpu_percent" in data
            assert data["cpu_percent"] == 45.0
            assert data["memory_percent"] == 60.0

            # Second metric push
            data2 = ws.receive_json()
            assert "cpu_percent" in data2

    # Monitor should have been called at least twice
    assert mock_monitor.collect_metrics.call_count >= 2
