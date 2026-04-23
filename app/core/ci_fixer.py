"""CI auto-fixer — uses Claude Code CLI to fix failing tests.

Calls Claude Code with targeted prompts for each failure, re-runs tests,
and retries up to MAX_ATTEMPTS times.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid

import httpx

from app.core.ci_runner import PROJECT_REGISTRY, run_project_tests
from app.core.ci_signal_dedup import compute_signature, record_lesson
from app.core.config import get_settings
from app.db.database import Database

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")


async def _open_ci_db() -> Database:
    """Open a fresh Database connection for CI lesson recording.

    Tests monkeypatch this to return a tmp_path-scoped Database.
    """
    db = Database(get_settings().db_path)
    await db.initialize()
    return db


async def post_lesson_summary_to_memory_api(
    *, type: str, name: str, description: str, content: str
) -> None:
    """Best-effort POST a lesson summary to the memory API.

    Silent on failure. The memory API rejects payloads with backslash/newline
    characters in JSON (it uses a strict parser), so the caller is responsible
    for keeping ``content`` on a single line.
    """
    try:
        settings = get_settings()
        base = settings.memory_api_base
        key = settings.memory_api_key
        if not base or not key:
            return
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{base}/memories",
                headers={"X-Memory-Key": key, "Content-Type": "application/json"},
                json={"type": type, "name": name,
                      "description": description, "content": content},
            )
    except Exception as exc:
        logger.warning("memory api post failed: %s", exc)


# ---------------------------------------------------------------------------
# Claude Code helpers (same pattern as app/api/claude_code.py)
# ---------------------------------------------------------------------------


def _load_claude_token() -> str | None:
    """Load CLAUDE_CODE_OAUTH_TOKEN from env or dotfiles."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    for f in [os.path.expanduser("~/.claude_env"), os.path.expanduser("~/.bashrc")]:
        try:
            with open(f) as fh:
                for line in fh:
                    if "CLAUDE_CODE_OAUTH_TOKEN=" in line:
                        return line.split("=", 1)[1].strip().strip("'\"")
        except FileNotFoundError:
            pass
    return None


def _find_claude() -> str | None:
    """Find Claude Code CLI binary."""
    if os.path.exists(CLAUDE_BIN):
        return CLAUDE_BIN
    return shutil.which("claude")


def _build_env() -> dict:
    """Build environment dict with OAuth token."""
    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    return env


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_fix_prompt(
    project: str,
    test_file: str,
    test_name: str,
    error: str,
    source_file: str | None = None,
    prev_errors: list[str] | None = None,
) -> str:
    """Build a prompt for Claude Code to fix a failing test.

    The prompt is in Turkish to match the existing codebase style.
    """
    lines = [
        f"Proje: {project}",
        f"Basarisiz test dosyasi: {test_file}",
        f"Test adi: {test_name}",
        "",
        "Hata mesaji:",
        error,
    ]

    if source_file:
        lines.extend(["", f"Ilgili kaynak dosya: {source_file}"])

    if prev_errors:
        lines.extend(["", "Onceki duzeltme denemelerinde alinan hatalar:"])
        for i, prev in enumerate(prev_errors, 1):
            lines.append(f"  Deneme {i}: {prev}")

    lines.extend([
        "",
        "Bu testi duzelt. Sadece gerekli dosyalari degistir.",
        "Testi tekrar calistirdigimda gecmesini saglayacak en kucuk degisikligi yap.",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Code caller
# ---------------------------------------------------------------------------


async def _call_claude_code(prompt: str, cwd: str) -> dict:
    """Call Claude Code CLI and return parsed result.

    Returns::

        {"answer": str, "session_id": str | None, "error": str | None}
    """
    binary = _find_claude()
    if not binary:
        return {"answer": "", "session_id": None, "error": "Claude Code CLI bulunamadi"}

    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", "5",
    ]

    logger.info("Calling Claude Code: cwd=%s", cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_build_env(),
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return {"answer": "", "session_id": None, "error": "Zaman asimi (5dk)"}
    except Exception as exc:
        return {"answer": "", "session_id": None, "error": str(exc)}

    raw = stdout.decode() if stdout else ""

    # Find JSON start (skip any preamble text)
    output = raw
    for i, ch in enumerate(raw):
        if ch in ("{", "["):
            output = raw[i:]
            break

    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return {
            "answer": raw[:500],
            "session_id": None,
            "error": f"JSON parse hatasi. stderr: {stderr.decode()[:200] if stderr else ''}",
        }

    # Extract fields -- handle both dict and list formats
    session_id = None
    answer = ""

    if isinstance(result, dict):
        session_id = result.get("session_id")
        answer = result.get("result", "")
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                if item.get("type") == "result":
                    session_id = item.get("session_id")
                    answer = item.get("result", "")
                elif item.get("type") == "system" and item.get("session_id"):
                    session_id = item["session_id"]

    return {"answer": answer, "session_id": session_id, "error": None}


# ---------------------------------------------------------------------------
# Fix loop
# ---------------------------------------------------------------------------


async def attempt_fix(
    project: str,
    test_file: str,
    test_name: str,
    error: str,
    source_file: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """Attempt to fix a failing test using Claude Code.

    Loops up to *max_attempts* times: builds prompt, calls Claude Code,
    re-runs tests, checks if the specific test passes.

    Returns::

        {
            "fixed": bool,
            "attempt": int,          # number of attempts made
            "project": str,
            "test_file": str,
            "test_name": str,
            "claude_responses": [...],
            "error": str | None,
        }
    """
    if project not in PROJECT_REGISTRY:
        return {
            "fixed": False,
            "attempt": 0,
            "project": project,
            "test_file": test_file,
            "test_name": test_name,
            "claude_responses": [],
            "error": f"Bilinmeyen proje: {project}",
        }

    run_uuid = uuid.uuid4().hex

    try:
        db = await _open_ci_db()
    except Exception as exc:
        logger.warning("CI lesson DB open failed: %s", exc)
        db = None

    try:
        cwd = PROJECT_REGISTRY[project]["path"]
        prev_errors: list[str] = []
        claude_responses: list[dict] = []

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "attempt_fix: %s / %s -- deneme %d/%d",
                project, test_name, attempt, max_attempts,
            )

            # 1. Build prompt (include previous errors for retries)
            prompt = build_fix_prompt(
                project=project,
                test_file=test_file,
                test_name=test_name,
                error=error if attempt == 1 else prev_errors[-1],
                source_file=source_file,
                prev_errors=prev_errors if attempt > 1 else None,
            )

            # 2. Call Claude Code
            claude_result = await _call_claude_code(prompt, cwd)
            claude_responses.append(claude_result)

            if claude_result.get("error"):
                logger.warning("Claude Code hatasi: %s", claude_result["error"])
                prev_errors.append(claude_result["error"])
                continue

            # 3. Re-run tests
            test_result = await run_project_tests(project)

            # Record the lesson for this attempt (before the passed/failed branches).
            current_error_text = error if attempt == 1 else prev_errors[-1]
            error_hash, signature = compute_signature(project, test_name, current_error_text)
            outcome = "passed" if test_result.get("failed", 0) == 0 else "failed"
            if db is not None:
                try:
                    await record_lesson(
                        db,
                        run_uuid=run_uuid,
                        project=project, test_name=test_name,
                        error_hash=error_hash, signature=signature,
                        raw_error=current_error_text,
                        attempt_num=attempt,
                        strategy="fix-direct",  # Phase 2 replaces this
                        context_lessons=None,
                        fix_diff=claude_result.get("answer"),
                        outcome=outcome,
                        duration_ms=None,
                    )
                except Exception as exc:
                    logger.warning("lesson record failed: %s", exc)

            # 4. Check if fixed
            if test_result.get("failed", 0) == 0:
                logger.info("Test duzeltildi! deneme=%d", attempt)
                # Post summary to memory API (single-line content -- memory API rejects \n in JSON)
                await post_lesson_summary_to_memory_api(
                    type="lesson_learned",
                    name=f"CI fix: {project}/{test_name}",
                    description=f"Attempt {attempt}, fix-direct - fixed",
                    content=(
                        f"Run {run_uuid[:8]}: {test_name} in {project} fixed on attempt {attempt}. "
                        f"Signature: {signature}. "
                        f"Diff length: {len(claude_result.get('answer') or '')} chars."
                    ),
                )
                return {
                    "fixed": True,
                    "attempt": attempt,
                    "project": project,
                    "test_file": test_file,
                    "test_name": test_name,
                    "claude_responses": claude_responses,
                    "error": None,
                }

            # 5. Still failing -- collect error for next attempt
            current_error = "Bilinmeyen hata"
            for failure in test_result.get("failures", []):
                if failure.get("test_name") == test_name or failure.get("test_file") == test_file:
                    current_error = failure.get("error", current_error)
                    break
            else:
                # If exact match not found, take first failure error
                if test_result.get("failures"):
                    current_error = test_result["failures"][0].get("error", current_error)

            prev_errors.append(current_error)
            logger.info("Hala basarisiz, deneme=%d, hata=%s", attempt, current_error[:100])

        # Max attempts exhausted
        return {
            "fixed": False,
            "attempt": max_attempts,
            "project": project,
            "test_file": test_file,
            "test_name": test_name,
            "claude_responses": claude_responses,
            "error": f"{max_attempts} deneme sonrasi duzeltilemedi",
        }
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception as exc:
                logger.warning("CI lesson DB close failed: %s", exc)
