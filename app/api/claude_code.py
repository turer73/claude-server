"""Claude Code API — runs claude CLI with session persistence."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.middleware.dependencies import require_auth, require_admin

router = APIRouter(prefix="/api/v1/claude", tags=["claude-code"])

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")


def _load_claude_token():
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
    if os.path.exists(CLAUDE_BIN):
        return CLAUDE_BIN
    return shutil.which("claude")


def _build_env():
    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    return env


class ClaudePromptRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None  # Resume a previous session
    continue_last: bool = False       # Continue most recent session
    model: Optional[str] = None
    max_turns: Optional[int] = 10
    cwd: Optional[str] = None


@router.get("/status", dependencies=[Depends(require_auth)])
async def claude_status():
    binary = _find_claude()
    if not binary:
        return {"available": False, "error": "Claude Code CLI bulunamadi"}
    proc = await asyncio.create_subprocess_exec(
        binary, "--version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    version = stdout.decode().strip() if stdout else "unknown"
    token = _load_claude_token()
    return {
        "available": True,
        "version": version,
        "authenticated": bool(os.environ.get("ANTHROPIC_API_KEY") or token),
        "binary": binary,
    }


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_claude(body: ClaudePromptRequest):
    binary = _find_claude()
    if not binary:
        return {"error": "Claude Code CLI bulunamadi"}

    cmd = [binary, "-p", body.prompt, "--output-format", "json", "--dangerously-skip-permissions"]

    # Session continuity
    if body.session_id:
        cmd.extend(["--resume", body.session_id])
    elif body.continue_last:
        cmd.append("--continue")

    if body.model:
        cmd.extend(["--model", body.model])
    if body.max_turns:
        cmd.extend(["--max-turns", str(body.max_turns)])

    cwd = body.cwd or os.path.expanduser("~")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=_build_env(), stdin=asyncio.subprocess.DEVNULL,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "Zaman asimi (5dk)"}

    raw = stdout.decode() if stdout else ""
    # Find JSON start
    output = raw
    for i, ch in enumerate(raw):
        if ch in ('{', '['):
            output = raw[i:]
            break

    try:
        result = json.loads(output)
        # Extract session_id from result
        session_id = None
        answer = ""
        cost = 0
        is_error = False

        if isinstance(result, dict):
            session_id = result.get("session_id")
            answer = result.get("result", "")
            cost = result.get("total_cost_usd", 0)
            is_error = result.get("is_error", False)
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    if item.get("type") == "result":
                        session_id = item.get("session_id")
                        answer = item.get("result", "")
                        cost = item.get("total_cost_usd", 0)
                        is_error = item.get("is_error", False)
                    elif item.get("type") == "system" and item.get("session_id"):
                        session_id = item["session_id"]

        return {
            "ok": not is_error,
            "result": answer,
            "cost": cost,
            "session_id": session_id,
        }
    except json.JSONDecodeError:
        return {"ok": True, "raw": raw, "stderr": stderr.decode() if stderr else ""}


@router.get("/sessions", dependencies=[Depends(require_admin)])
async def list_sessions():
    """List recent Claude Code sessions."""
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    if not os.path.isdir(sessions_dir):
        return {"sessions": []}

    sessions = []
    for fname in sorted(os.listdir(sessions_dir), reverse=True)[:20]:
        fpath = os.path.join(sessions_dir, fname)
        try:
            stat = os.stat(fpath)
            sessions.append({
                "id": fname.replace(".json", ""),
                "modified": stat.st_mtime,
                "size": stat.st_size,
            })
        except OSError:
            pass
    return {"sessions": sessions}


@router.get("/ui", dependencies=[Depends(require_auth)])
async def claude_ui():
    html_path = os.path.join(os.path.dirname(__file__), "..", "claude_ui", "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Claude UI bulunamadi</h1>", status_code=404)
