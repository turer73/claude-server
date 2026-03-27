import pytest
from app.core.monitor_agent import MonitorAgent


@pytest.fixture
def monitor():
    return MonitorAgent()


def test_collect_metrics(monitor):
    metrics = monitor.collect_metrics()
    assert "timestamp" in metrics
    assert "cpu_percent" in metrics
    assert "memory_percent" in metrics
    assert "disk_percent" in metrics
    assert "load_avg" in metrics
    assert "network_sent_mb" in metrics
    assert "network_recv_mb" in metrics
    assert isinstance(metrics["cpu_percent"], float)
    assert isinstance(metrics["load_avg"], list)


def test_check_alerts_no_alert(monitor):
    metrics = {
        "cpu_percent": 10.0,
        "memory_percent": 20.0,
        "disk_percent": 30.0,
        "temperature": 40.0,
    }
    thresholds = {"cpu_percent": 85, "memory_percent": 85, "disk_percent": 90, "temperature_c": 80}
    alerts = monitor.check_alerts(metrics, thresholds)
    assert len(alerts) == 0


def test_check_alerts_cpu_high(monitor):
    metrics = {
        "cpu_percent": 95.0,
        "memory_percent": 20.0,
        "disk_percent": 30.0,
        "temperature": 40.0,
    }
    thresholds = {"cpu_percent": 85, "memory_percent": 85, "disk_percent": 90, "temperature_c": 80}
    alerts = monitor.check_alerts(metrics, thresholds)
    assert len(alerts) >= 1
    assert alerts[0]["source"] == "cpu"
    assert alerts[0]["severity"] == "warning"


def test_check_alerts_multiple(monitor):
    metrics = {
        "cpu_percent": 95.0,
        "memory_percent": 90.0,
        "disk_percent": 95.0,
        "temperature": 85.0,
    }
    thresholds = {"cpu_percent": 85, "memory_percent": 85, "disk_percent": 90, "temperature_c": 80}
    alerts = monitor.check_alerts(metrics, thresholds)
    assert len(alerts) == 4


def test_metrics_snapshot_format(monitor):
    metrics = monitor.collect_metrics()
    # Should be serializable
    assert isinstance(metrics["timestamp"], str)
    assert len(metrics["load_avg"]) == 3
