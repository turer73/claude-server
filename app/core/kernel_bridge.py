"""Kernel bridge — wraps procfs access to the loaded Linux-AI kernel modules.

The live server runs three modules (proc_linux_ai, nf_linux_ai, usb_linux_ai).
proc_linux_ai exposes:
  - /proc/linux_ai         (read-only metrics: version, uptime, memory, load, cpu_count, ...)
  - /proc/linux_ai_config  (writable alert thresholds: alert_cpu/alert_mem/alert_disk)

There is NO governor/cpufreq/affinity/ioctl control in any loaded module (that was
a Linux-AI-OS `/dev/ai_ctl` capability that was not carried over). `set_governor`
therefore fails honestly with "not supported" instead of pretending to succeed.

Gracefully degrades when the module is not loaded.
"""

from __future__ import annotations

import os

from app.exceptions import KernelError

PROC_STATUS = "/proc/linux_ai"
PROC_CONFIG = "/proc/linux_ai_config"
PROC_PREFIX = "linux_ai_"

STATE_NAMES = {0: "stopped", 1: "running", 2: "training", 3: "error"}


class KernelBridge:
    """Interface to the loaded Linux-AI proc_linux_ai module. Safe without it."""

    def is_available(self) -> bool:
        return os.path.exists(PROC_STATUS)

    def read_proc_status(self) -> dict[str, str]:
        """Parse /proc/linux_ai ("linux_ai_<key> <value>" per line). The
        "linux_ai_" prefix is stripped so callers see e.g. "cpu_count"."""
        try:
            with open(PROC_STATUS) as f:
                result = {}
                for line in f:
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    key, val = parts[0].strip(), parts[1].strip()
                    if key.startswith(PROC_PREFIX):
                        key = key[len(PROC_PREFIX):]
                    result[key] = val
                return result
        except (FileNotFoundError, PermissionError, OSError):
            return {}

    def get_status(self) -> dict:
        if not self.is_available():
            return {
                "state": "unavailable",
                "governor": "not_supported",
                "cpu_count": os.cpu_count() or 1,
                "services": 0,
                "version": None,
                "kernel_module": False,
            }
        status = self.read_proc_status()
        return {
            # The module being loaded is itself the "running" signal; it reports
            # metrics, not a lifecycle state machine.
            "state": "running",
            # No governor/cpufreq concept exists in the loaded modules.
            "governor": "not_supported",
            "cpu_count": int(status.get("cpu_count", os.cpu_count() or 1)),
            "services": 0,
            "version": status.get("version"),
            "kernel_module": True,
        }

    def set_governor(self, mode: str) -> bool:
        """Not supported: no loaded module provides governor/cpufreq control.

        Fails honestly (502) rather than returning a fake success. Real governor
        control would require either a kernel module that exposes it (cf. the
        Linux-AI-OS /dev/ai_ctl ioctl) or a userspace cpufreq path."""
        raise KernelError(
            "Governor control is not supported by the loaded kernel modules "
            "(proc_linux_ai provides read-only metrics + alert thresholds only; "
            "no cpufreq/governor/ioctl interface)"
        )
