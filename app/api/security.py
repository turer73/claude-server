"""Security API — self-pentest job runner.

Goose extension (Phase 2) backs onto these endpoints. Whitelist source-of-truth
is `automation/self-pentest.domains` read at request time — no env-var
duplication, so [[correction-goose-extension-phase1-2026-05-27]] point 3 drift
risk is eliminated.

Endpoints:
  GET  /api/v1/security/pentest/targets      — whitelist
  POST /api/v1/security/pentest/run          — trigger scan for one domain
  GET  /api/v1/security/pentest/runs/{job}   — status + log tail

Auth: X-Memory-Key header (same as memory router). Rate-limited per
existing read/exec buckets.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.api.memory import verify_key
from app.middleware.dependencies import rate_limit_exec, rate_limit_read

ROOT = Path("/opt/linux-ai-server")
DOMAINS_FILE = ROOT / "automation" / "self-pentest.domains"
PENTEST_SCRIPT = ROOT / "automation" / "self-pentest.sh"
RUNS_DIR = ROOT / "logs" / "self-pentest" / "runs"

# Domain must look like a hostname: labels of a-z, 0-9, hyphen; dots between.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

router = APIRouter(
    prefix="/api/v1/security",
    tags=["security"],
    dependencies=[Depends(verify_key)],
)

# In-process job registry. Lost on uvicorn restart — acceptable for v1.
# If persistence becomes critical, move to claude_memory.db pentest_runs table.
_JOBS: dict[str, dict] = {}


def _load_targets() -> list[str]:
    """Read whitelist at request time — single source of truth."""
    if not DOMAINS_FILE.exists():
        return []
    out = []
    for line in DOMAINS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.lower())
    return out


class RunRequest(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if not _DOMAIN_RE.match(v):
            raise ValueError("invalid domain format")
        return v


class RunResponse(BaseModel):
    job_id: str
    domain: str
    status: Literal["running", "completed", "failed"]
    started_at: float


@router.get("/pentest/targets", dependencies=[Depends(rate_limit_read)])
def list_targets() -> dict:
    return {"targets": _load_targets(), "source": str(DOMAINS_FILE)}


@router.post("/pentest/run", dependencies=[Depends(rate_limit_exec)])
def run_scan(req: RunRequest) -> RunResponse:
    targets = _load_targets()
    if req.domain not in targets:
        raise HTTPException(
            status_code=400,
            detail=f"domain not in whitelist (source: {DOMAINS_FILE.name})",
        )
    if not PENTEST_SCRIPT.exists() or not os.access(PENTEST_SCRIPT, os.X_OK):
        raise HTTPException(status_code=500, detail="self-pentest.sh missing or not executable")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log_path = RUNS_DIR / f"{job_id}.log"
    log_fh = log_path.open("wb")
    try:
        proc = subprocess.Popen(  # noqa: S603 — script path constant, arg validated
            [str(PENTEST_SCRIPT), req.domain],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            start_new_session=True,
        )
    except OSError as e:
        log_fh.close()
        raise HTTPException(status_code=500, detail=f"spawn failed: {e}") from e

    started_at = time.time()
    _JOBS[job_id] = {
        "job_id": job_id,
        "domain": req.domain,
        "pid": proc.pid,
        "log_path": str(log_path),
        "log_fh": log_fh,
        "started_at": started_at,
        "status": "running",
        "exit_code": None,
        "_proc": proc,
    }
    return RunResponse(job_id=job_id, domain=req.domain, status="running", started_at=started_at)


@router.get("/pentest/runs/{job_id}", dependencies=[Depends(rate_limit_read)])
def get_run(job_id: str, tail: int = 200) -> dict:
    record = _JOBS.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="job not found")

    proc = record["_proc"]
    rc = proc.poll()
    if rc is None:
        record["status"] = "running"
    else:
        record["exit_code"] = rc
        record["status"] = "completed" if rc == 0 else "failed"
        # Close write handle now that the child exited.
        fh = record.get("log_fh")
        if fh and not fh.closed:
            try:
                fh.close()
            except OSError:
                pass

    tail_n = max(1, min(int(tail), 5000))
    log_lines: list[str] = []
    log_path = Path(record["log_path"])
    if log_path.exists():
        try:
            with log_path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                read_size = min(size, 128 * 1024)
                fh.seek(size - read_size, 0)
                data = fh.read().decode("utf-8", errors="replace")
                log_lines = data.splitlines()[-tail_n:]
        except OSError:
            pass

    return {
        "job_id": job_id,
        "domain": record["domain"],
        "status": record["status"],
        "exit_code": record["exit_code"],
        "started_at": record["started_at"],
        "pid": record["pid"],
        "log_tail": log_lines,
    }
