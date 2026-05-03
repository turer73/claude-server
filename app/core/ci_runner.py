"""CI test runner — run tests across all projects and parse results.

Supports local pytest, SSH-based pytest (VPS), and marks remote Windows
projects as skipped until n8n trigger integration is ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project registry
# ---------------------------------------------------------------------------

PROJECT_REGISTRY: dict[str, dict] = {
    # Tüm Node projeleri scripts/run-all-tests.sh ile aynı path/cmd
    # kullanir (2026-05-01: Windows -> Linux migrasyon, env=local).
    "panola": {
        "path": "/data/projects/panola",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "kuafor-panel": {
        "path": "/data/projects/kuafor/panel",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "kuafor-worker": {
        "path": "/data/projects/kuafor/worker",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "petvet": {
        "path": "/data/projects/petvet/web",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "renderhane": {
        "path": "/data/projects/renderhane",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "bilge-arena": {
        "path": "/data/projects/bilge-arena",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "koken-akademi": {
        "path": "/data/projects/koken-akademi/apps/api",
        "test_cmd": "npx vitest run --reporter=json",
        "env": "local",
        "framework": "vitest",
    },
    "klipper": {
        "path": "/opt/linux-ai-server",
        "test_cmd": "python3 -m pytest tests/ --tb=line -q",
        "env": "local",
        "framework": "pytest",
    },
    "panola-rag": {
        "path": "/opt/panola-rag",
        "test_cmd": "cd /opt/panola-rag && venv/bin/python -m pytest tests/ --tb=line -q",
        "env": "vps_ssh",
        "framework": "pytest",
    },
}

REQUIRED_KEYS = {"path", "test_cmd", "env", "framework"}

# VPS SSH connection details for panola-rag
VPS_SSH_HOST = "100.126.113.23"
VPS_SSH_USER = "root"
VPS_SSH_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Vitest JSON parser
# ---------------------------------------------------------------------------


def parse_vitest_json(raw: str) -> dict:
    """Parse vitest ``--reporter=json`` output into a normalised result dict.

    Returns::

        {
            "total": int,
            "passed": int,
            "failed": int,
            "duration_s": float,
            "failures": [{"test_file": str, "test_name": str, "error": str}, ...],
        }
    """
    # vitest-pool-workers (Cloudflare) prefixes [vpw:info]/[vpw:debug] log
    # lines around the JSON. Find the first '{' and last '}' that look like
    # the report object.
    def _extract_json(s: str) -> str:
        first = s.find('{"')
        if first < 0:
            first = s.find("{")
        last = s.rfind("}")
        if first >= 0 and last > first:
            return s[first : last + 1]
        return s

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_extract_json(raw))
        except json.JSONDecodeError as exc:
            logger.warning("vitest JSON parse failed: %s", exc)
            return {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "duration_s": 0.0,
                "failures": [],
                "error": f"JSON parse error: {exc}",
            }

    # vitest JSON structure: { testResults: [...], numTotalTests, ... }
    num_total = data.get("numTotalTests", 0)
    num_passed = data.get("numPassedTests", 0)
    num_failed = data.get("numFailedTests", 0)

    # Duration: vitest reports startTime (epoch ms) at top level, and
    # each testResult has a duration field.  We sum per-suite durations.
    total_duration_ms = 0
    for suite in data.get("testResults", []):
        # endTime - startTime at suite level, or fall back to per-test sums
        start = suite.get("startTime", 0)
        end = suite.get("endTime", 0)
        if end and start:
            total_duration_ms += end - start

    failures: list[dict] = []
    for suite in data.get("testResults", []):
        suite_file = suite.get("name", "unknown")
        for assertion in suite.get("assertionResults", []):
            if assertion.get("status") == "failed":
                error_messages = assertion.get("failureMessages", [])
                failures.append(
                    {
                        "test_file": suite_file,
                        "test_name": assertion.get("fullName", assertion.get("title", "unknown")),
                        "error": "\n".join(error_messages) if error_messages else "unknown error",
                    }
                )

    return {
        "total": num_total,
        "passed": num_passed,
        "failed": num_failed,
        "duration_s": round(total_duration_ms / 1000, 2),
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Pytest output parser
# ---------------------------------------------------------------------------

# Matches the summary line: "430 passed in 12.34s" or "5 failed, 425 passed in 12.34s"
# Also handles: "5 failed, 425 passed, 2 warnings in 12.34s"
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:(\d+) failed)?[,\s]*"
    r"(?:(\d+) passed)?[,\s]*"
    r"(?:(\d+) warnings?)?[,\s]*"
    r"(?:(\d+) errors?)?[,\s]*"
    r"in ([\d.]+)s"
)

# Matches failure lines like: "tests/test_foo.py:42: AssertionError: bad"
_PYTEST_FAILURE_RE = re.compile(
    r"^(tests?/\S+\.py):(\d+):\s+(.+)$", re.MULTILINE
)

# Matches FAILED summary lines: "FAILED tests/test_foo.py::test_name - error msg"
_PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+(\S+)::(\S+)\s*-\s*(.+)$", re.MULTILINE
)


def parse_pytest_output(raw: str) -> dict:
    """Parse pytest ``-q --tb=line`` output into a normalised result dict.

    Returns same shape as :func:`parse_vitest_json`.
    """
    failed = 0
    passed = 0
    duration = 0.0

    # Find the summary line (last line matching the pattern)
    for match in _PYTEST_SUMMARY_RE.finditer(raw):
        failed = int(match.group(1) or 0)
        passed = int(match.group(2) or 0)
        duration = float(match.group(5))

    total = passed + failed

    # Extract individual failure lines from --tb=line output
    failures: list[dict] = []
    seen = set()
    for match in _PYTEST_FAILURE_RE.finditer(raw):
        filepath = match.group(1)
        line_no = match.group(2)
        error_msg = match.group(3).strip()
        key = f"{filepath}:{line_no}"
        if key not in seen:
            seen.add(key)
            failures.append(
                {
                    "test_file": filepath,
                    "test_name": f"{filepath}:{line_no}",
                    "error": error_msg,
                }
            )

    # Also extract FAILED summary lines (more reliable for test names)
    for match in _PYTEST_FAILED_RE.finditer(raw):
        filepath = match.group(1)
        test_name = match.group(2)
        error_msg = match.group(3).strip()
        key = f"{filepath}::{test_name}"
        if key not in seen:
            seen.add(key)
            failures.append(
                {
                    "test_file": filepath,
                    "test_name": test_name,
                    "error": error_msg,
                }
            )

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "duration_s": duration,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


async def run_project_tests(project: str) -> dict:
    """Run tests for *project* and return a normalised result dict.

    Raises :class:`ValueError` if *project* is not in the registry.
    """
    if project not in PROJECT_REGISTRY:
        raise ValueError(
            f"Unknown project {project!r}. "
            f"Valid: {sorted(PROJECT_REGISTRY.keys())}"
        )

    cfg = PROJECT_REGISTRY[project]
    env = cfg["env"]
    framework = cfg["framework"]

    # --- remote_windows: skip for now ---
    if env == "remote_windows":
        return {
            "project": project,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "duration_s": 0,
            "failures": [],
            "skipped": True,
            "skip_reason": "remote_windows \u2014 requires n8n trigger",
        }

    start = time.monotonic()

    try:
        if env == "local":
            stdout, stderr, returncode = await _run_local(cfg)
        elif env == "vps_ssh":
            stdout, stderr, returncode = await _run_ssh(cfg)
        else:
            return {
                "project": project,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "duration_s": 0,
                "failures": [],
                "skipped": True,
                "skip_reason": f"unsupported env {env!r}",
            }
    except Exception as exc:
        elapsed = round(time.monotonic() - start, 2)
        logger.error("run_project_tests(%s) error: %s", project, exc)
        return {
            "project": project,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "duration_s": elapsed,
            "failures": [],
            "error": str(exc),
        }

    elapsed = round(time.monotonic() - start, 2)
    combined = stdout + "\n" + stderr

    # Parse output based on framework
    if framework == "pytest":
        result = parse_pytest_output(combined)
    elif framework == "vitest":
        result = parse_vitest_json(stdout)
    else:
        result = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "duration_s": elapsed,
            "failures": [],
            "error": f"unknown framework {framework!r}",
        }

    result["project"] = project
    result["duration_s"] = result.get("duration_s") or elapsed
    result["raw_output"] = combined[-2000:]  # keep last 2kB for debugging
    return result


async def _run_local(cfg: dict) -> tuple[str, str, int]:
    """Execute test command locally via asyncio subprocess."""
    cmd = cfg["test_cmd"]
    cwd = cfg["path"]
    logger.info("Running local: cd %s && %s", cwd, cmd)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(), timeout=300
    )
    return (
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
        proc.returncode or 0,
    )


async def _run_ssh(cfg: dict) -> tuple[str, str, int]:
    """Execute test command on VPS via SSH subprocess."""
    remote_cmd = cfg["test_cmd"]
    ssh_cmd = (
        f"ssh -o ConnectTimeout={VPS_SSH_TIMEOUT} "
        f"-o StrictHostKeyChecking=no "
        f"{VPS_SSH_USER}@{VPS_SSH_HOST} "
        f"'{remote_cmd}'"
    )
    logger.info("Running SSH: %s", ssh_cmd)

    proc = await asyncio.create_subprocess_shell(
        ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(), timeout=120
    )
    return (
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
        proc.returncode or 0,
    )
