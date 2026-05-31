"""Kernel bridge — wraps procfs + cpufreq sysfs access for the Linux-AI server.

The live server runs three modules (proc_linux_ai, nf_linux_ai, usb_linux_ai).
proc_linux_ai exposes:
  - /proc/linux_ai         (read-only metrics: version, uptime, memory, load, cpu_count, ...)
  - /proc/linux_ai_config  (writable alert thresholds: alert_cpu/alert_mem/alert_disk)

Governor control uses the STANDARD Linux cpufreq sysfs interface
(/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor) — NOT a custom kernel
module. The original Linux-AI-OS /dev/ai_ctl ioctl governor path was never carried
over; the cpufreq path is the real, supported mechanism on this hardware.

Gracefully degrades when the metrics module is not loaded.
"""

from __future__ import annotations

import glob
import os
import subprocess

from app.exceptions import KernelError

PROC_STATUS = "/proc/linux_ai"
PROC_CONFIG = "/proc/linux_ai_config"
PROC_PREFIX = "linux_ai_"

CPUFREQ_BASE = "/sys/devices/system/cpu"
CPUFREQ_CUR = f"{CPUFREQ_BASE}/cpu0/cpufreq/scaling_governor"
CPUFREQ_AVAIL = f"{CPUFREQ_BASE}/cpu0/cpufreq/scaling_available_governors"
CPUFREQ_GLOB = f"{CPUFREQ_BASE}/cpu*/cpufreq/scaling_governor"

STATE_NAMES = {0: "stopped", 1: "running", 2: "training", 3: "error"}


class KernelBridge:
    """Interface to the loaded Linux-AI proc_linux_ai module + cpufreq sysfs."""

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

    # --- cpufreq governor (standard sysfs, not a custom module) ---

    def available_governors(self) -> list[str]:
        try:
            with open(CPUFREQ_AVAIL) as f:
                return f.read().split()
        except (FileNotFoundError, PermissionError, OSError):
            return []

    def current_governor(self) -> str | None:
        try:
            with open(CPUFREQ_CUR) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def get_status(self) -> dict:
        module_loaded = self.is_available()
        status = self.read_proc_status() if module_loaded else {}
        return {
            # The module being loaded is itself the "running" signal; it reports
            # metrics, not a lifecycle state machine.
            "state": "running" if module_loaded else "unavailable",
            # Live cpufreq governor (independent of the metrics module).
            "governor": self.current_governor() or "unknown",
            "cpu_count": int(status.get("cpu_count", os.cpu_count() or 1)),
            "services": 0,
            "version": status.get("version"),
            "kernel_module": module_loaded,
        }

    def set_governor(self, mode: str) -> bool:
        """Set the cpufreq governor on every CPU via the standard sysfs interface.

        Validates `mode` against the kernel-reported available governors, then
        writes through `sudo tee` (the service runs as a non-root user and
        scaling_governor is root-owned). Verifies the change took, otherwise
        raises — never returns a fake success."""
        avail = self.available_governors()
        if not avail:
            raise KernelError("cpufreq governor control is not available on this host")
        if mode not in avail:
            raise KernelError(
                f"Governor '{mode}' not available; supported: {', '.join(avail)}"
            )
        targets = glob.glob(CPUFREQ_GLOB)
        if not targets:
            raise KernelError("No cpufreq scaling_governor sysfs nodes found")
        try:
            proc = subprocess.run(
                ["sudo", "-n", "tee", *targets],
                input=mode,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as e:
            raise KernelError(f"Failed to set governor: {e}")
        if proc.returncode != 0:
            raise KernelError(
                f"Failed to set governor (sudo tee rc={proc.returncode}): "
                f"{proc.stderr.strip() or 'permission denied?'}"
            )
        actual = self.current_governor()
        if actual != mode:
            raise KernelError(f"Governor write did not take (still '{actual}')")
        return True
