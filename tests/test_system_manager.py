"""SystemManager — sistem bilgisi, process, signal testleri."""

from unittest.mock import patch

import pytest

from app.core.system_manager import SystemManager
from app.exceptions import NotFoundError, ShellExecutionError


@pytest.fixture
def sm():
    return SystemManager()


def test_get_system_info(sm):
    info = sm.get_system_info()
    assert "hostname" in info
    assert "cpu_count" in info
    assert info["cpu_count"] >= 1
    assert "memory_total_mb" in info
    assert info["memory_total_mb"] > 0
    assert "disk_total_gb" in info
    assert "load_avg" in info
    assert len(info["load_avg"]) == 3


def test_get_system_info_loadavg_fallback(sm):
    """os.getloadavg olmayan platformda fallback."""
    with patch("os.getloadavg", side_effect=OSError("not supported")):
        info = sm.get_system_info()
        assert info["load_avg"] == [0.0, 0.0, 0.0]


def test_get_processes(sm):
    procs = sm.get_processes(limit=5)
    assert isinstance(procs, list)
    assert len(procs) <= 5
    if procs:
        p = procs[0]
        assert "pid" in p
        assert "name" in p
        assert "cpu_percent" in p
        assert "memory_mb" in p


def test_get_processes_sort_by_memory(sm):
    procs = sm.get_processes(limit=5, sort_by="memory")
    assert isinstance(procs, list)
    if len(procs) >= 2:
        assert procs[0]["memory_mb"] >= procs[1]["memory_mb"]


def test_get_processes_sort_by_cpu(sm):
    procs = sm.get_processes(limit=5, sort_by="cpu")
    assert isinstance(procs, list)


def test_send_signal_not_found(sm):
    with pytest.raises(NotFoundError, match="not found"):
        sm.send_signal(9999999)


def test_send_signal_access_denied(sm):
    with patch("psutil.Process") as mock_proc:
        import psutil

        mock_proc.return_value.send_signal.side_effect = psutil.AccessDenied(pid=1)
        with pytest.raises(ShellExecutionError, match="Permission denied"):
            sm.send_signal(1, signal=15)
