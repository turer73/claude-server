"""Monitor agent — real-time system metrics collection and alerting."""

from __future__ import annotations

from datetime import datetime, timezone

import psutil


class MonitorAgent:
    """Collects system metrics and checks alert thresholds."""

    def collect_metrics(self) -> dict:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()

        try:
            import os
            load = list(os.getloadavg())
        except (OSError, AttributeError):
            load = [0.0, 0.0, 0.0]

        temp = None
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    if entries:
                        temp = entries[0].current
                        break
        except (AttributeError, RuntimeError):
            pass

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
            "temperature": temp,
            "load_avg": load,
            "network_sent_mb": round(net.bytes_sent / (1024 * 1024), 2),
            "network_recv_mb": round(net.bytes_recv / (1024 * 1024), 2),
        }

    def check_alerts(self, metrics: dict, thresholds: dict) -> list[dict]:
        alerts = []
        checks = [
            ("cpu", "cpu_percent", "cpu_percent"),
            ("memory", "memory_percent", "memory_percent"),
            ("disk", "disk_percent", "disk_percent"),
            ("temperature", "temperature", "temperature_c"),
        ]
        for source, metric_key, threshold_key in checks:
            value = metrics.get(metric_key)
            limit = thresholds.get(threshold_key)
            if value is not None and limit is not None and value > limit:
                alerts.append({
                    "severity": "warning" if value <= limit + 10 else "critical",
                    "source": source,
                    "message": f"{source} at {value}% (threshold: {limit}%)",
                    "value": value,
                    "threshold": limit,
                })
        return alerts
