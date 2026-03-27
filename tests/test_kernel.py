"""Tests for kernel bridge core service and REST API."""

import pytest
from unittest.mock import patch, mock_open
from app.core.kernel_bridge import KernelBridge


@pytest.fixture
def bridge():
    return KernelBridge()


def test_governor_name_mapping(bridge):
    assert bridge.governor_name(0) == "performance"
    assert bridge.governor_name(1) == "powersave"
    assert bridge.governor_name(4) == "ai_adaptive"
    assert bridge.governor_name(99) == "unknown"


def test_governor_id_mapping(bridge):
    assert bridge.governor_id("performance") == 0
    assert bridge.governor_id("powersave") == 1
    assert bridge.governor_id("nonexistent") == -1


def test_read_sysfs_success(bridge):
    with patch("builtins.open", mock_open(read_data="0.3.3\n")):
        result = bridge.read_sysfs("version")
        assert result == "0.3.3"


def test_read_sysfs_not_found(bridge):
    with patch("builtins.open", side_effect=FileNotFoundError):
        result = bridge.read_sysfs("nonexistent")
        assert result is None


def test_read_proc_status(bridge):
    mock_content = "state: running\ngovernor: performance\ncpu_count: 4\nservices: 2\n"
    with patch("builtins.open", mock_open(read_data=mock_content)):
        result = bridge.read_proc_status()
        assert result["state"] == "running"
        assert result["governor"] == "performance"
        assert result["cpu_count"] == "4"


def test_read_proc_status_file_missing(bridge):
    with patch("builtins.open", side_effect=FileNotFoundError):
        result = bridge.read_proc_status()
        assert result == {}


def test_is_available_false(bridge):
    with patch("os.path.exists", return_value=False):
        assert bridge.is_available() is False


def test_is_available_true(bridge):
    with patch("os.path.exists", return_value=True):
        assert bridge.is_available() is True


def test_get_status_without_module(bridge):
    with patch("os.path.exists", return_value=False):
        status = bridge.get_status()
        assert status["state"] == "unavailable"
        assert status["kernel_module"] is False
        assert "cpu_count" in status


def test_get_status_with_module(bridge):
    mock_content = "state: running\ngovernor: performance\ncpu_count: 4\nservices: 2\n"
    with patch("os.path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data=mock_content)):
            status = bridge.get_status()
            assert status["state"] == "running"
            assert status["kernel_module"] is True


def test_set_governor_without_module(bridge):
    from app.exceptions import KernelError
    with patch("os.path.exists", return_value=False):
        with pytest.raises(KernelError):
            bridge.set_governor("performance")


def test_set_governor_invalid_mode(bridge):
    from app.exceptions import KernelError
    with patch("os.path.exists", return_value=True):
        with pytest.raises(KernelError, match="Unknown governor"):
            bridge.set_governor("turbo")


# --- API Integration Tests ---

@pytest.mark.anyio
async def test_kernel_status_api(client):
    resp = await client.get("/api/v1/kernel/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "state" in data
    assert "governor" in data
    assert "cpu_count" in data


@pytest.mark.anyio
async def test_kernel_governor_api(client):
    resp = await client.get("/api/v1/kernel/governor")
    assert resp.status_code == 200
    assert "governor" in resp.json()
