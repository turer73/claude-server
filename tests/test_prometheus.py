import pytest
from app.core.prometheus_exporter import PrometheusExporter


@pytest.fixture
def exporter():
    return PrometheusExporter()


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
