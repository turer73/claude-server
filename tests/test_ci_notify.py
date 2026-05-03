"""Tests for app/core/ci_notify.py — Telegram notification helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core import ci_notify


@pytest.fixture
def telegram_settings(monkeypatch):
    """Force the settings cache to return a stub with telegram creds set."""
    from app.core.config import get_settings

    get_settings.cache_clear()

    class _Stub:
        telegram_bot_token = "test-bot-token"
        telegram_chat_id = "test-chat-id"

    monkeypatch.setattr(ci_notify, "get_settings", lambda: _Stub())
    yield
    get_settings.cache_clear()


async def test_no_creds_returns_silently(monkeypatch):
    """When token or chat_id is empty, the notifier is a no-op."""

    class _Stub:
        telegram_bot_token = ""
        telegram_chat_id = ""

    monkeypatch.setattr(ci_notify, "get_settings", lambda: _Stub())

    # No httpx call should happen — no exception should be raised either
    with patch("app.core.ci_notify.httpx.AsyncClient") as mock_client:
        await ci_notify.notify_ci_result(total=10, passed=10, failed=0, projects=[])
        mock_client.assert_not_called()


async def test_all_passed_emits_success_message(telegram_settings):
    """Run with 0 failures gets the green emoji + ALL PASSED status."""
    captured = {}

    async def _fake_post(url, json):
        captured["url"] = url
        captured["body"] = json

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = _fake_post
    mock_client.__aexit__.return_value = None

    with patch("app.core.ci_notify.httpx.AsyncClient", return_value=mock_client):
        await ci_notify.notify_ci_result(
            total=10,
            passed=10,
            failed=0,
            projects=[{"project": "p1", "passed": 10, "total": 10, "failed": 0}],
            run_id=42,
        )

    assert "test-bot-token" in captured["url"]
    text = captured["body"]["text"]
    assert "ALL PASSED" in text
    assert "10/10" in text
    assert "Run #42" in text


async def test_failed_run_lists_failures(telegram_settings):
    captured = {}

    async def _fake_post(url, json):
        captured["body"] = json

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = _fake_post
    mock_client.__aexit__.return_value = None

    projects = [
        {"project": "alpha", "passed": 5, "total": 5, "failed": 0},
        {"project": "beta", "passed": 3, "total": 5, "failed": 2, "fix_result": "auto-fixed"},
    ]

    with patch("app.core.ci_notify.httpx.AsyncClient", return_value=mock_client):
        await ci_notify.notify_ci_result(total=10, passed=8, failed=2, projects=projects, trigger="cron")

    text = captured["body"]["text"]
    assert "2 FAILED" in text
    assert "beta" in text
    assert "auto-fixed" in text
    assert "alpha" in text  # listed under "All projects"
    assert "cron" in text


async def test_send_failure_is_swallowed(telegram_settings, caplog):
    """If httpx raises, the error is logged but the function does not propagate."""
    mock_client = AsyncMock()
    mock_client.__aenter__.side_effect = RuntimeError("network down")

    with patch("app.core.ci_notify.httpx.AsyncClient", return_value=mock_client):
        await ci_notify.notify_ci_result(total=1, passed=1, failed=0, projects=[])

    assert any("Failed to send CI Telegram" in r.message for r in caplog.records)
