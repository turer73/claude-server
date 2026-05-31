"""Tests for the kernel bridge core service and REST API.

The bridge talks to the loaded proc_linux_ai module (/proc/linux_ai read-only
metrics). There is no governor/cpufreq control on this server, so set_governor
fails honestly. These tests assert that honest behaviour against the real
/proc/linux_ai format ("linux_ai_<key> <value>" per line).
"""

from unittest.mock import mock_open, patch

import pytest

from app.core.kernel_bridge import KernelBridge


@pytest.fixture
def bridge():
    return KernelBridge()


def test_is_available_false(bridge):
    with patch("os.path.exists", return_value=False):
        assert bridge.is_available() is False


def test_is_available_true(bridge):
    with patch("os.path.exists", return_value=True):
        assert bridge.is_available() is True


def test_read_proc_status_parses_real_format(bridge):
    # /proc/linux_ai uses space-separated "linux_ai_<key> <value>"; the prefix
    # is stripped by the bridge.
    mock_content = (
        "linux_ai_version 1\n"
        "linux_ai_cpu_count 16\n"
        "linux_ai_load_1m 0.16\n"
        "linux_ai_threshold_cpu 85\n"
    )
    with patch("builtins.open", mock_open(read_data=mock_content)):
        result = bridge.read_proc_status()
        assert result["version"] == "1"
        assert result["cpu_count"] == "16"
        assert result["load_1m"] == "0.16"
        assert result["threshold_cpu"] == "85"


def test_read_proc_status_file_missing(bridge):
    with patch("builtins.open", side_effect=FileNotFoundError):
        assert bridge.read_proc_status() == {}


def test_get_status_without_module(bridge):
    with patch("os.path.exists", return_value=False):
        status = bridge.get_status()
        assert status["state"] == "unavailable"
        assert status["kernel_module"] is False
        assert status["governor"] == "not_supported"
        assert "cpu_count" in status


def test_get_status_with_module(bridge):
    mock_content = "linux_ai_version 1\nlinux_ai_cpu_count 16\n"
    with patch("os.path.exists", return_value=True), patch("builtins.open", mock_open(read_data=mock_content)):
        status = bridge.get_status()
        assert status["state"] == "running"
        assert status["kernel_module"] is True
        assert status["cpu_count"] == 16
        assert status["version"] == "1"
        # No governor concept in the loaded module — must be reported honestly.
        assert status["governor"] == "not_supported"


def test_set_governor_always_not_supported(bridge):
    """No loaded module provides governor control; set_governor must fail
    honestly (never a fake success), whether or not a module is present."""
    from app.exceptions import KernelError

    for present in (True, False):
        with patch("os.path.exists", return_value=present):
            with pytest.raises(KernelError, match="not supported"):
                bridge.set_governor("performance")


# --- API Integration Tests ---


@pytest.mark.anyio
async def test_kernel_status_api(client, auth_headers):
    resp = await client.get("/api/v1/kernel/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert "governor" in data
    assert "cpu_count" in data


@pytest.mark.anyio
async def test_kernel_governor_api(client, auth_headers):
    resp = await client.get("/api/v1/kernel/governor", headers=auth_headers)
    assert resp.status_code == 200
    assert "governor" in resp.json()
