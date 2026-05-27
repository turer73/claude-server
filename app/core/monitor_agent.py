"""Monitor agent — real-time system metrics collection and alerting."""

from __future__ import annotations

from datetime import UTC, datetime

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

        # Temperature: gercek CPU sensor sec. Onceki versiyon ilk chip'i (acpitz =
        # motherboard ACPI, idle 20°C sabit) okuyordu, 5+ aydir tum CPU temp
        # alarm'lari yutuyordu. Klipper Ryzen 7 → k10temp (Tctl) gercek CPU.
        # 2026-05-27 fix: chip oncelik listesi.
        temp = None
        _CPU_TEMP_CHIPS = ("k10temp", "coretemp", "cpu_thermal", "zenpower")
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                # 1) Gercek CPU sensor (vendor-spesifik)
                for chip in _CPU_TEMP_CHIPS:
                    if chip in temps and temps[chip]:
                        temp = temps[chip][0].current
                        break
                # 2) Fallback: acpitz haric ilk anlamli sensor
                if temp is None:
                    for name, entries in temps.items():
                        if name == "acpitz":
                            continue
                        if entries and entries[0].current and entries[0].current > 25:
                            temp = entries[0].current
                            break
        except (AttributeError, RuntimeError):
            pass

        return {
            "timestamp": datetime.now(UTC).isoformat(),
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
                alerts.append(
                    {
                        "severity": "warning" if value <= limit + 10 else "critical",
                        "source": source,
                        "message": f"{source} at {value}% (threshold: {limit}%)",
                        "value": value,
                        "threshold": limit,
                    }
                )
        return alerts
