"""Prometheus metrics exporter — /metrics endpoint in Prometheus text format."""

from __future__ import annotations

import glob
import os
import time

import psutil


class PrometheusExporter:
    """Export system metrics in Prometheus text exposition format."""

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _gpu_metrics(self) -> list[str]:
        """amdgpu utilization/VRAM/temp from sysfs (read-only; no kernel module —
        replaces the Linux-AI-OS proc-GPUIO C++ idea). Empty when no GPU sysfs."""
        busy, vram_used, vram_total, temp = [], [], [], []
        for gpu_path in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
            card = gpu_path.split("/")[4]
            dev = os.path.dirname(gpu_path)
            b = self._read_int(gpu_path)
            if b is None:
                continue
            busy.append((card, b))
            vu = self._read_int(f"{dev}/mem_info_vram_used")
            if vu is not None:
                vram_used.append((card, vu))
            vt = self._read_int(f"{dev}/mem_info_vram_total")
            if vt is not None:
                vram_total.append((card, vt))
            for tp in sorted(glob.glob(f"{dev}/hwmon/hwmon*/temp1_input")):
                t = self._read_int(tp)
                if t is not None:
                    temp.append((card, t / 1000.0))
                break

        lines: list[str] = []
        families = [
            ("linux_ai_gpu_busy_percent", "GPU utilization percentage (amdgpu)", busy),
            ("linux_ai_gpu_vram_used_bytes", "GPU VRAM used in bytes", vram_used),
            ("linux_ai_gpu_vram_total_bytes", "GPU VRAM total in bytes", vram_total),
            ("linux_ai_gpu_temp_celsius", "GPU temperature in Celsius", temp),
        ]
        for name, help_text, values in families:
            if not values:
                continue
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for card, val in values:
                lines.append(f'{name}{{card="{card}"}} {val}')
        return lines

    def _llm_metrics(self) -> list[str]:
        """LLM çağrı-metrikleri (rag_metrics.db/llm_calls, son 24s) — #100224-audit: LLMCore
        9 çağrı-yeri gözlemsizdi. backend/ok bazlı çağrı-sayısı + ortalama-latency. Fail-safe."""
        import sqlite3

        db = os.environ.get("RAG_METRICS_DB", "/opt/linux-ai-server/data/rag_metrics.db")
        try:
            conn = sqlite3.connect(db, timeout=2)
            try:
                rows = conn.execute(
                    "SELECT backend, ok, COUNT(*), COALESCE(AVG(latency_ms), 0) "
                    "FROM llm_calls WHERE ts > datetime('now', '-24 hours') GROUP BY backend, ok"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []  # tablo/DB yok → metrik yok (fail-safe)
        if not rows:
            return []
        out = [
            "# HELP linux_ai_llm_calls_total LLM calls (24h) by backend and ok",
            "# TYPE linux_ai_llm_calls_total gauge",
            "# HELP linux_ai_llm_latency_ms_avg Avg LLM latency ms (24h) by backend and ok",
            "# TYPE linux_ai_llm_latency_ms_avg gauge",
        ]
        for backend, ok, n, avg_lat in rows:
            b = str(backend or "unknown")
            out.append(f'linux_ai_llm_calls_total{{backend="{b}",ok="{int(ok or 0)}"}} {n}')
            out.append(f'linux_ai_llm_latency_ms_avg{{backend="{b}",ok="{int(ok or 0)}"}} {avg_lat:.1f}')
        return out

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

        # GPU (amdgpu sysfs; emitted only when a GPU exposes the nodes)
        lines.extend(self._gpu_metrics())

        # LLM çağrı-metrikleri (rag_metrics.db/llm_calls; tablo yoksa boş)
        lines.extend(self._llm_metrics())

        return "\n".join(lines) + "\n"
