"""Kernel bridge — wraps ioctl/procfs/sysfs access to Linux-AI kernel module.

Gracefully degrades when kernel module is not loaded.
"""

from __future__ import annotations

import os

from app.exceptions import KernelError

DEVICE_PATH = "/dev/ai_ctl"
PROC_STATUS = "/proc/ai_status"
PROC_CONFIG = "/proc/ai_config"
SYSFS_BASE = "/sys/ai"

GOV_NAMES = {
    0: "performance",
    1: "powersave",
    2: "ondemand",
    3: "conservative",
    4: "ai_adaptive",
}
GOV_BY_NAME = {v: k for k, v in GOV_NAMES.items()}

STATE_NAMES = {0: "stopped", 1: "running", 2: "training", 3: "error"}


class KernelBridge:
    """Interface to Linux-AI kernel module. Safe to call without the module."""

    def is_available(self) -> bool:
        return os.path.exists(DEVICE_PATH)

    def governor_name(self, mode_id: int) -> str:
        return GOV_NAMES.get(mode_id, "unknown")

    def governor_id(self, name: str) -> int:
        return GOV_BY_NAME.get(name, -1)

    def read_sysfs(self, attr: str) -> str | None:
        path = f"{SYSFS_BASE}/{attr}"
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def write_sysfs(self, attr: str, value: str) -> bool:
        path = f"{SYSFS_BASE}/{attr}"
        try:
            with open(path, "w") as f:
                f.write(value)
            return True
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise KernelError(f"Failed to write sysfs {attr}: {e}")

    def read_proc_status(self) -> dict[str, str]:
        try:
            with open(PROC_STATUS) as f:
                result = {}
                for line in f:
                    if ":" in line:
                        key, _, val = line.partition(":")
                        result[key.strip()] = val.strip()
                return result
        except (FileNotFoundError, PermissionError, OSError):
            return {}

    def write_proc_config(self, key: str, value: str) -> bool:
        try:
            with open(PROC_CONFIG, "w") as f:
                f.write(f"{key}={value}\n")
            return True
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise KernelError(f"Failed to write proc config {key}: {e}")

    def get_status(self) -> dict:
        if not self.is_available():
            return {
                "state": "unavailable",
                "governor": "unknown",
                "cpu_count": os.cpu_count() or 1,
                "services": 0,
                "version": None,
                "kernel_module": False,
            }
        status = self.read_proc_status()
        return {
            "state": status.get("state", "unknown"),
            "governor": status.get("governor", "unknown"),
            "cpu_count": int(status.get("cpu_count", os.cpu_count() or 1)),
            "services": int(status.get("services", 0)),
            "version": self.read_sysfs("version"),
            "kernel_module": True,
        }

    def set_governor(self, mode: str) -> bool:
        if not self.is_available():
            raise KernelError("Kernel module not loaded")
        gov_id = self.governor_id(mode)
        if gov_id < 0:
            raise KernelError(f"Unknown governor mode: {mode}")
        return self.write_proc_config("governor", mode)
