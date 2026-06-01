from unittest.mock import mock_open, patch

import pytest

from app.core.prometheus_exporter import PrometheusExporter


@pytest.fixture
def exporter():
    return PrometheusExporter()


def test_read_int_valid(exporter):
    with patch("builtins.open", mock_open(read_data="42\n")):
        assert exporter._read_int("/x") == 42


def test_read_int_missing(exporter):
    with patch("builtins.open", side_effect=FileNotFoundError):
        assert exporter._read_int("/x") is None


def test_gpu_metrics_format(exporter):
    # Machine-independent: fake one amdgpu card via mocked sysfs.
    def fake_glob(pattern):
        return ["/sys/class/drm/card1/device/gpu_busy_percent"] if "gpu_busy" in pattern else []

    with patch("glob.glob", side_effect=fake_glob), patch.object(PrometheusExporter, "_read_int", return_value=7):
        lines = exporter._gpu_metrics()
        assert "# TYPE linux_ai_gpu_busy_percent gauge" in lines
        assert any('linux_ai_gpu_busy_percent{card="card1"} 7' in line for line in lines)


def test_gpu_metrics_absent(exporter):
    with patch("glob.glob", return_value=[]):
        assert exporter._gpu_metrics() == []


def test_export_format(exporter):
    output = exporter.export()
    assert isinstance(output, str)
    assert "# HELP" in output
    assert "# TYPE" in output


def test_contains_cpu_metric(exporter):
    output = exporter.export()
    assert "linux_ai_cpu_percent" in output


def test_contains_memory_metric(exporter):
    output = exporter.export()
    assert "linux_ai_memory_percent" in output


def test_contains_disk_metric(exporter):
    output = exporter.export()
    assert "linux_ai_disk_percent" in output


def test_contains_uptime(exporter):
    output = exporter.export()
    assert "linux_ai_uptime_seconds" in output


def test_valid_prometheus_format(exporter):
    """Each metric line should be 'name value' or 'name{labels} value'."""
    output = exporter.export()
    for line in output.strip().split("\n"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(" ")
        assert len(parts) >= 2, f"Invalid line: {line}"
        # Value should be numeric
        try:
            float(parts[-1])
        except ValueError:
            pytest.fail(f"Non-numeric value in: {line}")
