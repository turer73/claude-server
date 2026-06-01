"""Tests for app/api/security.py — pentest job runner (Goose extension backing)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

HEADERS = {"X-Memory-Key": "test-security-key"}


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def pentest_env(tmp_path, monkeypatch):
    """Isolate security.py module state to tmp_path. Returns the tmp dir."""
    domains_file = tmp_path / "self-pentest.domains"
    domains_file.write_text(
        "# comment\npanola.app\npetvet.panola.app\n\n  KUAFOR.panola.app  \n"  # mixed case + whitespace — _load_targets normalizes
    )

    fake_script = tmp_path / "self-pentest.sh"
    fake_script.write_text('#!/usr/bin/env bash\necho "scanning $1"\necho "done"\nexit 0\n')
    _make_executable(fake_script)

    runs_dir = tmp_path / "runs"

    from app.api import memory as memory_mod
    from app.api import security as mod

    # ROOT is the hardcoded prod install path (/opt/linux-ai-server) used as the
    # spawn cwd; it does not exist on CI runners, so the Popen would fail with
    # ENOENT -> HTTP 500. Point it at tmp_path for the test.
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "DOMAINS_FILE", domains_file)
    monkeypatch.setattr(mod, "PENTEST_SCRIPT", fake_script)
    monkeypatch.setattr(mod, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(memory_mod, "MEMORY_API_KEY", "test-security-key")
    mod._JOBS.clear()
    yield tmp_path
    mod._JOBS.clear()


async def _wait_for_completion(client, job_id, timeout_s=5.0):
    """Poll get_run until status != 'running'."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        resp = await client.get(f"/api/v1/security/pentest/runs/{job_id}", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "running":
            return body
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"job {job_id} did not complete in {timeout_s}s")
        await asyncio.sleep(0.1)


async def test_targets_returns_whitelist(client, pentest_env):
    resp = await client.get("/api/v1/security/pentest/targets", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "panola.app" in body["targets"]
    assert "petvet.panola.app" in body["targets"]
    # mixed-case + whitespace + comment + blank handled, lower-cased
    assert "kuafor.panola.app" in body["targets"]
    assert "source" in body


async def test_auth_rejects_wrong_key(client, pentest_env):
    resp = await client.get("/api/v1/security/pentest/targets", headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401


async def test_run_rejects_off_whitelist_domain(client, pentest_env):
    resp = await client.post(
        "/api/v1/security/pentest/run",
        json={"domain": "evil.example.com"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "whitelist" in resp.json()["detail"]


async def test_run_rejects_invalid_domain_format(client, pentest_env):
    for bad in ["not a domain", "spaces here.com", "../etc/passwd", "a;b.com", "no-tld"]:
        resp = await client.post("/api/v1/security/pentest/run", json={"domain": bad}, headers=HEADERS)
        assert resp.status_code == 422, f"expected 422 for {bad!r}, got {resp.status_code}"


async def test_run_spawns_and_completes(client, pentest_env):
    resp = await client.post(
        "/api/v1/security/pentest/run",
        json={"domain": "panola.app"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "panola.app"
    assert body["status"] == "running"
    job_id = body["job_id"]
    assert len(job_id) == 12

    final = await _wait_for_completion(client, job_id)
    assert final["status"] == "completed"
    assert final["exit_code"] == 0
    assert any("scanning panola.app" in line for line in final["log_tail"])
    assert any("done" in line for line in final["log_tail"])

    # Log file should be on disk in the per-job dir.
    log_path = pentest_env / "runs" / f"{job_id}.log"
    assert log_path.exists()


async def test_run_missing_script_returns_500(client, pentest_env, monkeypatch):
    from app.api import security as mod

    monkeypatch.setattr(mod, "PENTEST_SCRIPT", pentest_env / "does-not-exist.sh")
    resp = await client.post(
        "/api/v1/security/pentest/run",
        json={"domain": "panola.app"},
        headers=HEADERS,
    )
    assert resp.status_code == 500


async def test_get_run_unknown_job_returns_404(client, pentest_env):
    resp = await client.get("/api/v1/security/pentest/runs/nosuchjob123", headers=HEADERS)
    assert resp.status_code == 404


async def test_run_records_failure_exit_code(client, pentest_env):
    from app.api import security as mod

    failing = mod.PENTEST_SCRIPT  # exec-capable path chosen by the fixture
    failing.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 7\n")
    _make_executable(failing)

    resp = await client.post(
        "/api/v1/security/pentest/run",
        json={"domain": "panola.app"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    final = await _wait_for_completion(client, job_id)
    assert final["status"] == "failed"
    assert final["exit_code"] == 7
    assert any("boom" in line for line in final["log_tail"])


# ---------- auth alias: X-Pentest-Key accepted alongside X-Memory-Key ----------


async def test_x_pentest_key_header_accepted(client, pentest_env):
    """Generic OSS package sends X-Pentest-Key; backend must accept it."""
    resp = await client.get(
        "/api/v1/security/pentest/targets",
        headers={"X-Pentest-Key": "test-security-key"},
    )
    assert resp.status_code == 200


async def test_x_pentest_key_wrong_value_rejected(client, pentest_env):
    resp = await client.get(
        "/api/v1/security/pentest/targets",
        headers={"X-Pentest-Key": "nope"},
    )
    assert resp.status_code == 401


# ---------- findings adapter ----------


@pytest.fixture
def findings_db(tmp_path, monkeypatch, pentest_env):
    """Per-test memory DB so the findings adapter has a discoveries table."""
    import sqlite3

    from tests.test_memory_api import MEMORY_SCHEMA

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(MEMORY_SCHEMA)
    # Three rows: one bug active, one bug completed, one different type
    conn.execute(
        "INSERT INTO discoveries (project, type, title, details, status) VALUES ('panola.app', 'bug', 'open CSP gap', 'detail A', 'active')"
    )
    conn.execute(
        "INSERT INTO discoveries (project, type, title, details, status) "
        "VALUES ('panola.app', 'bug', 'old fixed thing', 'detail B', 'completed')"
    )
    conn.execute(
        "INSERT INTO discoveries (project, type, title, details, status) VALUES ('panola.app', 'fix', 'not a bug', 'detail C', 'active')"
    )
    conn.commit()
    conn.close()

    from app.api import memory as memory_mod

    monkeypatch.setattr(memory_mod, "DB_PATH", str(db_path))
    return db_path


async def test_findings_pins_type_bug(client, findings_db):
    resp = await client.get("/api/v1/security/pentest/findings", headers=HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    # Only the active bug — completed bug filtered (status='active' default),
    # 'fix' row excluded (type pinned to 'bug').
    assert len(rows) == 1
    assert rows[0]["title"] == "open CSP gap"
    assert rows[0]["type"] == "bug"


async def test_findings_status_filter_passthrough(client, findings_db):
    resp = await client.get("/api/v1/security/pentest/findings?status=completed", headers=HEADERS)
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["title"] == "old fixed thing"


async def test_finding_get_by_id_returns_full_record(client, findings_db):
    resp = await client.get("/api/v1/security/pentest/findings/1", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["details"] == "detail A"


async def test_finding_resolve_marks_completed(client, findings_db):
    resp = await client.put("/api/v1/security/pentest/findings/1/resolve", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    # Verify via the list endpoint — row 1 should no longer be in active
    resp2 = await client.get("/api/v1/security/pentest/findings", headers=HEADERS)
    titles = [r["title"] for r in resp2.json()]
    assert "open CSP gap" not in titles
