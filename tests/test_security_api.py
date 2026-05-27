"""Tests for app/api/security.py — pentest job runner (Goose extension backing)."""

from __future__ import annotations

import asyncio
import stat

import pytest

HEADERS = {"X-Memory-Key": "test-security-key"}


@pytest.fixture
def pentest_env(tmp_path, monkeypatch):
    """Isolate security.py module state to tmp_path. Returns the tmp dir."""
    domains_file = tmp_path / "self-pentest.domains"
    domains_file.write_text(
        "# comment\n"
        "panola.app\n"
        "petvet.panola.app\n"
        "\n"
        "  KUAFOR.panola.app  \n"  # mixed case + whitespace — _load_targets normalizes
    )

    fake_script = tmp_path / "self-pentest.sh"
    fake_script.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"scanning $1\"\n"
        "echo \"done\"\n"
        "exit 0\n"
    )
    fake_script.chmod(fake_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    runs_dir = tmp_path / "runs"

    from app.api import memory as memory_mod
    from app.api import security as mod

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
    resp = await client.get(
        "/api/v1/security/pentest/targets", headers={"X-Memory-Key": "wrong"}
    )
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
        resp = await client.post(
            "/api/v1/security/pentest/run", json={"domain": bad}, headers=HEADERS
        )
        assert resp.status_code == 422, f"expected 422 for {bad!r}, got {resp.status_code}"


async def test_run_spawns_and_completes(client, pentest_env):
    resp = await client.post(
        "/api/v1/security/pentest/run",
        json={"domain": "panola.app"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
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
    resp = await client.get(
        "/api/v1/security/pentest/runs/nosuchjob123", headers=HEADERS
    )
    assert resp.status_code == 404


async def test_run_records_failure_exit_code(client, pentest_env):
    failing = pentest_env / "self-pentest.sh"
    failing.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 7\n")
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

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
