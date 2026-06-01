"""Tests for the kernel bridge core service and REST API.

The bridge reads metrics from the loaded proc_linux_ai module (/proc/linux_ai)
and controls the CPU governor through the standard cpufreq sysfs interface
(/sys/.../scaling_governor) via `sudo tee` (the service runs non-root).
"""

import subprocess
from unittest.mock import mock_open, patch

import pytest

from app.core.kernel_bridge import KernelBridge
from app.exceptions import KernelError


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
    # /proc/linux_ai uses space-separated "linux_ai_<key> <value>"; prefix stripped.
    mock_content = "linux_ai_version 1\nlinux_ai_cpu_count 16\nlinux_ai_load_1m 0.16\n"
    with patch("builtins.open", mock_open(read_data=mock_content)):
        result = bridge.read_proc_status()
        assert result["version"] == "1"
        assert result["cpu_count"] == "16"
        assert result["load_1m"] == "0.16"


def test_read_proc_status_file_missing(bridge):
    with patch("builtins.open", side_effect=FileNotFoundError):
        assert bridge.read_proc_status() == {}


def test_get_status_without_module(bridge):
    with patch.object(bridge, "is_available", return_value=False), patch.object(bridge, "current_governor", return_value="powersave"):
        status = bridge.get_status()
        assert status["state"] == "unavailable"
        assert status["kernel_module"] is False
        # cpufreq governor is independent of the metrics module.
        assert status["governor"] == "powersave"
        assert "cpu_count" in status


def test_get_status_with_module(bridge):
    with (
        patch.object(bridge, "is_available", return_value=True),
        patch.object(bridge, "read_proc_status", return_value={"version": "1", "cpu_count": "16"}),
        patch.object(bridge, "current_governor", return_value="performance"),
    ):
        status = bridge.get_status()
        assert status["state"] == "running"
        assert status["kernel_module"] is True
        assert status["cpu_count"] == 16
        assert status["version"] == "1"
        assert status["governor"] == "performance"


def test_set_governor_success(bridge):
    with (
        patch.object(bridge, "available_governors", return_value=["performance", "powersave"]),
        patch("glob.glob", return_value=["/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"]),
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "performance", "")),
        patch.object(bridge, "current_governor", return_value="performance"),
    ):
        assert bridge.set_governor("performance") is True


def test_set_governor_not_available(bridge):
    # Mode is a valid Linux governor but not offered by this hardware/driver.
    with patch.object(bridge, "available_governors", return_value=["performance", "powersave"]):
        with pytest.raises(KernelError, match="not available"):
            bridge.set_governor("ondemand")


def test_set_governor_no_cpufreq(bridge):
    with patch.object(bridge, "available_governors", return_value=[]):
        with pytest.raises(KernelError, match="not available"):
            bridge.set_governor("performance")


def test_set_governor_write_fails(bridge):
    with (
        patch.object(bridge, "available_governors", return_value=["performance", "powersave"]),
        patch("glob.glob", return_value=["/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"]),
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")),
    ):
        with pytest.raises(KernelError, match="Failed to set governor"):
            bridge.set_governor("performance")


def test_set_governor_verify_fails(bridge):
    # sudo tee succeeds but the governor did not actually change -> honest raise.
    with (
        patch.object(bridge, "available_governors", return_value=["performance", "powersave"]),
        patch("glob.glob", return_value=["/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"]),
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")),
        patch.object(bridge, "current_governor", return_value="powersave"),
    ):
        with pytest.raises(KernelError, match="did not take"):
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
