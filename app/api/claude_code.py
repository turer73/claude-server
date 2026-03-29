"""Claude Code API — runs claude CLI in print mode and streams responses."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

from app.middleware.dependencies import require_auth, require_admin

router = APIRouter(prefix="/api/v1/claude", tags=["claude-code"])

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")

# Load OAuth token from ~/.bashrc env or file
def _load_claude_token():
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    env_file = os.path.expanduser("~/.claude_env")
    bashrc = os.path.expanduser("~/.bashrc")
    for f in [env_file, bashrc]:
        try:
            with open(f) as fh:
                for line in fh:
                    if "CLAUDE_CODE_OAUTH_TOKEN=" in line:
                        return line.split("=", 1)[1].strip().strip("'\"")
        except FileNotFoundError:
            pass
    return None


class ClaudePromptRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    max_turns: Optional[int] = 10
    cwd: Optional[str] = None


def _find_claude() -> str | None:
    """Find claude binary."""
    if os.path.exists(CLAUDE_BIN):
        return CLAUDE_BIN
    return shutil.which("claude")


@router.get("/status", dependencies=[Depends(require_auth)])
async def claude_status():
    """Check if Claude Code CLI is available and authenticated."""
    binary = _find_claude()
    if not binary:
        return {"available": False, "error": "Claude Code CLI bulunamadi"}

    proc = await asyncio.create_subprocess_exec(
        binary, "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    version = stdout.decode().strip() if stdout else "unknown"

    # Check auth
    token = _load_claude_token()
    has_auth = bool(os.environ.get("ANTHROPIC_API_KEY") or token)

    return {
        "available": True,
        "version": version,
        "authenticated": has_auth,
        "binary": binary,
    }


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_claude(body: ClaudePromptRequest):
    """Run Claude Code in print mode (non-streaming)."""
    binary = _find_claude()
    if not binary:
        return {"error": "Claude Code CLI bulunamadi"}

    cmd = [binary, "-p", body.prompt, "--output-format", "json"]
    if body.model:
        cmd.extend(["--model", body.model])
    if body.max_turns:
        cmd.extend(["--max-turns", str(body.max_turns)])

    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    cwd = body.cwd or os.path.expanduser("~")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "Zaman asimi (5dk)"}

    output = stdout.decode() if stdout else ""
    # Try parse JSON
    try:
        result = json.loads(output)
        return {"ok": True, "result": result}
    except json.JSONDecodeError:
        return {"ok": True, "raw": output, "stderr": stderr.decode() if stderr else ""}


@router.post("/stream", dependencies=[Depends(require_admin)])
async def stream_claude(body: ClaudePromptRequest):
    """Run Claude Code with streaming output via SSE."""
    binary = _find_claude()
    if not binary:
        return {"error": "Claude Code CLI bulunamadi"}

    cmd = [binary, "-p", body.prompt, "--output-format", "stream-json"]
    if body.model:
        cmd.extend(["--model", body.model])
    if body.max_turns:
        cmd.extend(["--max-turns", str(body.max_turns)])

    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    cwd = body.cwd or os.path.expanduser("~")

    async def event_stream():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
        )

        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=300
                )
                if not line:
                    break
                text = line.decode().strip()
                if text:
                    yield f"data: {text}\n\n"
        except asyncio.TimeoutError:
            proc.kill()
            yield f"data: {json.dumps({'type': 'error', 'error': 'Zaman asimi'})}\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve the Claude Code chat UI
@router.get("/ui", dependencies=[Depends(require_auth)])
async def claude_ui():
    """Serve Claude Code chat interface."""
    html_path = os.path.join(os.path.dirname(__file__), "..", "claude_ui", "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Claude UI bulunamadi</h1>", status_code=404)
