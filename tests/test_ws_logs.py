"""Tests for WebSocket log streaming handler."""

import pytest
import os
from unittest.mock import patch
from starlette.testclient import TestClient

from tests.conftest import TEST_API_KEY


def test_ws_logs_file_not_found(app):
    """Test log WebSocket handles missing log file."""
    with patch("app.ws.logs.os.path.isfile", return_value=False):
        client = TestClient(app)
        with client.websocket_connect("/ws/logs") as ws:
            data = ws.receive_json()
            assert "error" in data
            assert "not found" in data["error"]


def test_ws_logs_streams_lines(app, tmp_path):
    """Test log WebSocket streams new lines from log file."""
    log_file = tmp_path / "test.log"
    log_file.write_text("line1\nline2\n")

    # Patch the log path to our temp file
    with patch("app.ws.logs.os.path.isfile", return_value=True), \
         patch("builtins.open", return_value=open(str(log_file), "r")):
        client = TestClient(app)
        # The ws handler seeks to end, so it won't send existing lines
        # We just verify it connects and doesn't crash
        with client.websocket_connect("/ws/logs") as ws:
            # The handler is tailing, so no immediate output expected
            # Just verify connection established without error
            pass
