"""Tests for app/api/projects.py — multi-project health/audit/sync."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.api import projects as projects_module


@pytest.fixture
def real_git_projects(monkeypatch):
    """Replace PROJECTS with one entry pointing at this repo (a real git checkout)
    plus one missing path to exercise both code paths."""
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [
            {"name": "self", "path": "/opt/linux-ai-server", "type": "python"},
            {"name": "missing", "path": "/data/projects/__nope__", "type": "node"},
        ],
    )


async def test_health_default_projects(client, auth_headers):
    """The default PROJECTS list has many non-existent paths in CI; the endpoint
    must still respond and report exists=False per project."""
    resp = await client.get("/api/v1/projects/health", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "timestamp" in body
    assert isinstance(body["projects"], list)
    assert len(body["projects"]) >= 1


async def test_health_with_real_repo(client, auth_headers, real_git_projects):
    """Exercise _git_info + _git_status against this repo."""
    resp = await client.get("/api/v1/projects/health", headers=auth_headers)
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()["projects"]}
    assert by_name["self"]["exists"] is True
    assert "git" in by_name["self"]
    assert "git_status" in by_name["self"]
    assert by_name["missing"]["exists"] is False


async def test_audit_default(client, auth_headers):
    resp = await client.get("/api/v1/projects/audit", headers=auth_headers)
    assert resp.status_code == 200
    assert "audits" in resp.json()


async def test_audit_python_skipped(client, auth_headers, real_git_projects):
    resp = await client.get("/api/v1/projects/audit", headers=auth_headers)
    body = resp.json()
    # Python projects always return skip from _dep_audit
    assert body["audits"]["self"]["status"] == "skip"


async def test_audit_node_no_lockfile(client, auth_headers, monkeypatch, tmp_path):
    """Node project without lockfile returns skip."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "bare-node", "path": str(bare), "type": "node"}],
    )
    resp = await client.get("/api/v1/projects/audit", headers=auth_headers)
    assert resp.json()["audits"]["bare-node"]["status"] == "skip"


async def test_audit_node_with_lockfile_ok(client, auth_headers, monkeypatch, tmp_path):
    """Node project with lockfile — mock npm audit returning no vulns."""
    proj = tmp_path / "node-proj"
    proj.mkdir()
    (proj / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "n", "path": str(proj), "type": "node"}],
    )

    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"metadata": {"vulnerabilities": {"high": 0, "critical": 0}}})

    with patch("app.api.projects.subprocess.run", return_value=fake):
        resp = await client.get("/api/v1/projects/audit", headers=auth_headers)

    assert resp.json()["audits"]["n"]["status"] == "ok"


async def test_audit_node_with_vulns(client, auth_headers, monkeypatch, tmp_path):
    proj = tmp_path / "vuln-proj"
    proj.mkdir()
    (proj / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "v", "path": str(proj), "type": "node"}],
    )

    fake = MagicMock()
    fake.returncode = 1
    fake.stdout = json.dumps({"metadata": {"vulnerabilities": {"high": 2, "critical": 1}}})

    with patch("app.api.projects.subprocess.run", return_value=fake):
        resp = await client.get("/api/v1/projects/audit", headers=auth_headers)

    body = resp.json()["audits"]["v"]
    assert body["status"] == "warning"
    assert body["high"] == 2
    assert body["critical"] == 1


async def test_audit_node_invalid_json(client, auth_headers, monkeypatch, tmp_path):
    proj = tmp_path / "bad-proj"
    proj.mkdir()
    (proj / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "b", "path": str(proj), "type": "node"}],
    )

    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "not valid json {{{"

    with patch("app.api.projects.subprocess.run", return_value=fake):
        resp = await client.get("/api/v1/projects/audit", headers=auth_headers)

    assert resp.json()["audits"]["b"]["status"] == "unknown"


async def test_audit_subprocess_exception(client, auth_headers, monkeypatch, tmp_path):
    proj = tmp_path / "err-proj"
    proj.mkdir()
    (proj / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "e", "path": str(proj), "type": "node"}],
    )

    with patch("app.api.projects.subprocess.run", side_effect=OSError("boom")):
        resp = await client.get("/api/v1/projects/audit", headers=auth_headers)

    assert resp.json()["audits"]["e"]["status"] == "error"


async def test_audit_monorepo_subdir_lockfile(client, auth_headers, monkeypatch, tmp_path):
    """Monorepo: lockfile lives at <root>/web/package-lock.json."""
    root = tmp_path / "monorepo"
    web = root / "web"
    web.mkdir(parents=True)
    (web / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "m", "path": str(root), "type": "node"}],
    )

    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"metadata": {"vulnerabilities": {"high": 0, "critical": 0}}})

    with patch("app.api.projects.subprocess.run", return_value=fake):
        resp = await client.get("/api/v1/projects/audit", headers=auth_headers)

    assert resp.json()["audits"]["m"]["status"] == "ok"


async def test_sync_skip_missing(client, auth_headers, monkeypatch):
    """Missing paths report status=skip."""
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "ghost", "path": "/data/projects/__ghost__", "type": "node"}],
    )
    resp = await client.post("/api/v1/projects/sync", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["results"]["ghost"]["status"] == "skip"


async def test_sync_ok_path(client, auth_headers, monkeypatch, tmp_path):
    """git pull returns rc=0 → status=ok."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "p", "path": str(proj), "type": "node"}],
    )

    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "Already up to date."

    with patch("app.api.projects.subprocess.run", return_value=fake):
        resp = await client.post("/api/v1/projects/sync", headers=auth_headers)

    assert resp.json()["results"]["p"]["status"] == "ok"


async def test_sync_subprocess_exception(client, auth_headers, monkeypatch, tmp_path):
    proj = tmp_path / "e"
    proj.mkdir()
    monkeypatch.setattr(
        projects_module,
        "PROJECTS",
        [{"name": "e", "path": str(proj), "type": "node"}],
    )

    with patch("app.api.projects.subprocess.run", side_effect=OSError("denied")):
        resp = await client.post("/api/v1/projects/sync", headers=auth_headers)

    assert resp.json()["results"]["e"]["status"] == "error"


async def test_last_test_result_reads_latest(monkeypatch, tmp_path):
    """_last_test_result picks the newest /tmp/test-results-*.json."""
    f1 = tmp_path / "test-results-1.json"
    f1.write_text(json.dumps({"v": 1}))
    f2 = tmp_path / "test-results-2.json"
    f2.write_text(json.dumps({"v": 2}))

    with patch("app.api.projects.glob.glob" if False else "glob.glob", return_value=[str(f2), str(f1)]):
        result = projects_module._last_test_result()
    assert result == {"v": 2}


async def test_last_test_result_missing_returns_none(monkeypatch):
    with patch("glob.glob", return_value=[]):
        assert projects_module._last_test_result() is None


async def test_health_unauthenticated(client):
    """Endpoint requires auth — no token → 401."""
    resp = await client.get("/api/v1/projects/health")
    assert resp.status_code == 401
