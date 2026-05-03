import pytest

from app.core.system_manager import SystemManager


@pytest.fixture
def mgr():
    return SystemManager()


def test_get_system_info(mgr):
    info = mgr.get_system_info()
    assert "hostname" in info
    assert "cpu_count" in info
    assert "memory_total_mb" in info
    assert "disk_total_gb" in info
    assert "uptime_seconds" in info
    assert info["cpu_count"] > 0
    assert info["memory_total_mb"] > 0


def test_get_processes(mgr):
    procs = mgr.get_processes(limit=5)
    assert isinstance(procs, list)
    assert len(procs) <= 5
    if procs:
        p = procs[0]
        assert "pid" in p
        assert "name" in p
        assert "cpu_percent" in p


def test_get_processes_default_limit(mgr):
    procs = mgr.get_processes()
    assert len(procs) <= 20


def test_get_processes_sort_by_cpu(mgr):
    procs = mgr.get_processes(limit=5, sort_by="cpu")
    assert isinstance(procs, list)


def test_get_processes_sort_by_memory(mgr):
    procs = mgr.get_processes(limit=5, sort_by="memory")
    assert isinstance(procs, list)
