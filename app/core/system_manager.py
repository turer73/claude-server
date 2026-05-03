"""System manager — processes, services, system info via psutil."""

from __future__ import annotations

import os
import platform
import time

import psutil

from app.exceptions import NotFoundError, ShellExecutionError


class SystemManager:
    """System management operations."""

    def get_system_info(self) -> dict:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        try:
            load = list(os.getloadavg())
        except (OSError, AttributeError):
            load = [0.0, 0.0, 0.0]

        return {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "kernel": platform.release(),
            "uptime_seconds": time.time() - psutil.boot_time(),
            "cpu_count": psutil.cpu_count() or 1,
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_total_mb": mem.total // (1024 * 1024),
            "memory_used_mb": mem.used // (1024 * 1024),
            "memory_percent": mem.percent,
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_percent": disk.percent,
            "load_avg": load,
        }

    def get_processes(self, limit: int = 20, sort_by: str = "cpu") -> list[dict]:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status", "username"]):
            try:
                info = p.info
                mem_mb = (info["memory_info"].rss / (1024 * 1024)) if info.get("memory_info") else 0.0
                procs.append(
                    {
                        "pid": info["pid"],
                        "name": info["name"] or "unknown",
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                        "memory_mb": round(mem_mb, 1),
                        "status": info.get("status") or "unknown",
                        "user": info.get("username") or "unknown",
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        key = "cpu_percent" if sort_by == "cpu" else "memory_mb"
        procs.sort(key=lambda x: x[key], reverse=True)
        return procs[:limit]

    def send_signal(self, pid: int, signal: int = 15) -> bool:
        try:
            proc = psutil.Process(pid)
            proc.send_signal(signal)
            return True
        except psutil.NoSuchProcess:
            raise NotFoundError(f"Process {pid} not found")
        except psutil.AccessDenied:
            raise ShellExecutionError(f"Permission denied for PID {pid}")
