#!/usr/bin/env python3
"""
task_worker.py — MVP autonomous task executor.
Polls /api/v1/memory/queue?status=pending&target_device=<host>,
claims one task atomically, executes ONLY whitelist-matching commands,
writes back exit code + stdout/stderr.

Safety: regex whitelist hard-coded. Any unmatched command marked failed
with stderr="not in whitelist" — never executed. AI-based escalation
(memory 295) extends this with confidence scoring.

Usage: python3 task_worker.py [--once]
  --once   process at most one task and exit (for cron / smoke test)
"""
import os
import re
import sys
import json
import time
import socket
import subprocess
import urllib.request
import urllib.error

API_BASE = os.environ.get("MEMORY_API", "http://127.0.0.1:8420/api/v1/memory")
API_KEY = os.environ.get("MEMORY_KEY", "")
if not API_KEY:
    print("FATAL: MEMORY_KEY env var required (set in /etc/task-worker.env)", flush=True)
    sys.exit(1)
HOSTNAME = socket.gethostname()
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_SEC", "10"))
EXEC_TIMEOUT = int(os.environ.get("WORKER_EXEC_TIMEOUT", "30"))

# Whitelist — read-only / safe diagnostic commands only.
# Each pattern must match the FULL command (anchored).
WHITELIST = [
    r"^echo\s+[\w\s\-\.]{0,200}$",
    r"^ls(\s+-[la]+)?(\s+/[\w\-/\.]{0,100})?$",
    r"^cat\s+/proc/(loadavg|uptime|meminfo|cpuinfo|version)$",
    r"^df(\s+-[hH])?$",
    r"^free(\s+-[mh])?$",
    r"^uptime$",
    r"^ps(\s+(aux|-ef))?$",
    r"^date(\s+-u)?$",
    r"^hostname$",
    r"^whoami$",
    r"^uname(\s+-[arn])?$",
    r"^pwd$",
]
WHITELIST_RX = [re.compile(p) for p in WHITELIST]


def log(msg: str) -> None:
    """Single-line stderr log with timestamp."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def http_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Memory-Key", API_KEY)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        log(f"HTTP error {method} {path}: {e}")
        return 0, None


def is_safe(command: str) -> bool:
    """Whitelist check — command must match at least one pattern fully."""
    cmd = command.strip()
    return any(rx.match(cmd) for rx in WHITELIST_RX)


def run_command(command: str) -> tuple[int, str, str]:
    """Execute via /bin/sh -c with hard timeout. Returns (rc, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT,
            check=False,
        )
        return result.returncode, result.stdout[:5000], result.stderr[:5000]
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {EXEC_TIMEOUT}s"
    except Exception as e:
        return 1, "", f"exec error: {e}"


def process_one() -> bool:
    """Pick + claim + run + writeback one task. Returns True if a task was processed."""
    # Find candidate
    code, tasks = http_request("GET", f"/queue?status=pending&target_device={HOSTNAME}&limit=1")
    if code != 200 or not tasks:
        return False

    task = tasks[0]
    task_id = task["id"]
    command = task["command"]

    # Atomic claim
    code, claimed = http_request("PUT", f"/queue/{task_id}/claim", {"claimed_by": HOSTNAME})
    if code == 409:
        log(f"task {task_id} already claimed by another worker")
        return False
    if code != 200:
        log(f"claim failed task {task_id} code={code}")
        return False

    log(f"claimed task {task_id}: {command[:80]}")

    # Whitelist gate
    if not is_safe(command):
        http_request("PUT", f"/queue/{task_id}/result", {
            "exit_code": 126,
            "stdout": "",
            "stderr": "command not in whitelist - human approval required",
            "status": "failed",
        })
        log(f"task {task_id} REJECTED (not whitelisted)")
        return True

    # Execute + writeback
    rc, stdout, stderr = run_command(command)
    status = "completed" if rc == 0 else "failed"
    http_request("PUT", f"/queue/{task_id}/result", {
        "exit_code": rc,
        "stdout": stdout,
        "stderr": stderr,
        "status": status,
    })
    log(f"task {task_id} {status} rc={rc}")
    return True


def main() -> int:
    once = "--once" in sys.argv
    log(f"worker starting host={HOSTNAME} once={once} poll={POLL_INTERVAL}s")
    try:
        if once:
            processed = process_one()
            return 0 if processed else 2
        while True:
            try:
                process_one()
            except Exception as e:
                log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log("worker stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
