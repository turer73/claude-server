"""Prometheus metrics exporter — /metrics endpoint in Prometheus text format."""

from __future__ import annotations

import time
import psutil


class PrometheusExporter:
    """Export system metrics in Prometheus text exposition format."""

    def export(self) -> str:
        lines: list[str] = []

        # CPU
        cpu = psutil.cpu_percent(interval=0.1)
        lines.append("# HELP linux_ai_cpu_percent Current CPU usage percentage")
        lines.append("# TYPE linux_ai_cpu_percent gauge")
        lines.append(f"linux_ai_cpu_percent {cpu}")

        # Per-core CPU
        per_cpu = psutil.cpu_percent(interval=0, percpu=True)
        lines.append("# HELP linux_ai_cpu_core_percent Per-core CPU usage")
        lines.append("# TYPE linux_ai_cpu_core_percent gauge")
        for i, pct in enumerate(per_cpu):
            lines.append(f'linux_ai_cpu_core_percent{{core="{i}"}} {pct}')

        # Memory
        mem = psutil.virtual_memory()
        lines.append("# HELP linux_ai_memory_percent Memory usage percentage")
        lines.append("# TYPE linux_ai_memory_percent gauge")
        lines.append(f"linux_ai_memory_percent {mem.percent}")
        lines.append("# HELP linux_ai_memory_used_bytes Memory used in bytes")
        lines.append("# TYPE linux_ai_memory_used_bytes gauge")
        lines.append(f"linux_ai_memory_used_bytes {mem.used}")
        lines.append("# HELP linux_ai_memory_total_bytes Memory total in bytes")
        lines.append("# TYPE linux_ai_memory_total_bytes gauge")
        lines.append(f"linux_ai_memory_total_bytes {mem.total}")

        # Disk
        disk = psutil.disk_usage("/")
        lines.append("# HELP linux_ai_disk_percent Disk usage percentage")
        lines.append("# TYPE linux_ai_disk_percent gauge")
        lines.append(f"linux_ai_disk_percent {disk.percent}")
        lines.append("# HELP linux_ai_disk_used_bytes Disk used in bytes")
        lines.append("# TYPE linux_ai_disk_used_bytes gauge")
        lines.append(f"linux_ai_disk_used_bytes {disk.used}")

        # Network
        net = psutil.net_io_counters()
        lines.append("# HELP linux_ai_network_sent_bytes Total bytes sent")
        lines.append("# TYPE linux_ai_network_sent_bytes counter")
        lines.append(f"linux_ai_network_sent_bytes {net.bytes_sent}")
        lines.append("# HELP linux_ai_network_recv_bytes Total bytes received")
        lines.append("# TYPE linux_ai_network_recv_bytes counter")
        lines.append(f"linux_ai_network_recv_bytes {net.bytes_recv}")

        # Uptime
        uptime = time.time() - psutil.boot_time()
        lines.append("# HELP linux_ai_uptime_seconds System uptime in seconds")
        lines.append("# TYPE linux_ai_uptime_seconds gauge")
        lines.append(f"linux_ai_uptime_seconds {uptime:.0f}")

        # Process count
        lines.append("# HELP linux_ai_process_count Number of running processes")
        lines.append("# TYPE linux_ai_process_count gauge")
        lines.append(f"linux_ai_process_count {len(psutil.pids())}")

        # Load average
        try:
            import os
            load = os.getloadavg()
            lines.append("# HELP linux_ai_load_avg System load average")
            lines.append("# TYPE linux_ai_load_avg gauge")
            lines.append(f'linux_ai_load_avg{{period="1m"}} {load[0]}')
            lines.append(f'linux_ai_load_avg{{period="5m"}} {load[1]}')
            lines.append(f'linux_ai_load_avg{{period="15m"}} {load[2]}')
        except (OSError, AttributeError):
            pass

        return "\n".join(lines) + "\n"
