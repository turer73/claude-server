"""Tests for CI test runner core — registry, parsers, and runner."""

import json

import pytest

from app.core.ci_runner import (
    PROJECT_REGISTRY,
    REQUIRED_KEYS,
    parse_pytest_output,
    parse_vitest_json,
    run_project_tests,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestProjectRegistry:
    def test_has_all_projects(self):
        # 2026-05-03: Windows hosts retired, Node projects migrated to local
        # /data/projects/*. koken-akademi added.
        expected = {
            "panola",
            "kuafor-panel",
            "kuafor-worker",
            "petvet",
            "renderhane",
            "bilge-arena",
            "koken-akademi",
            "klipper",
            "panola-rag",
        }
        assert set(PROJECT_REGISTRY.keys()) == expected

    def test_each_project_has_required_keys(self):
        for name, cfg in PROJECT_REGISTRY.items():
            missing = REQUIRED_KEYS - set(cfg.keys())
            assert not missing, f"{name} missing keys: {missing}"

    def test_env_values_are_valid(self):
        valid_envs = {"local", "vps_ssh", "remote_windows"}
        for name, cfg in PROJECT_REGISTRY.items():
            assert cfg["env"] in valid_envs, f"{name} has invalid env: {cfg['env']}"

    def test_framework_values_are_valid(self):
        valid_frameworks = {"pytest", "vitest"}
        for name, cfg in PROJECT_REGISTRY.items():
            assert cfg["framework"] in valid_frameworks, f"{name} has invalid framework: {cfg['framework']}"

    def test_klipper_is_local(self):
        assert PROJECT_REGISTRY["klipper"]["env"] == "local"

    def test_panola_rag_is_vps_ssh(self):
        assert PROJECT_REGISTRY["panola-rag"]["env"] == "vps_ssh"

    def test_node_projects_are_local(self):
        node_projects = [
            "panola",
            "kuafor-panel",
            "kuafor-worker",
            "petvet",
            "renderhane",
            "bilge-arena",
            "koken-akademi",
        ]
        for name in node_projects:
            assert PROJECT_REGISTRY[name]["env"] == "local"


# ---------------------------------------------------------------------------
# parse_vitest_json
# ---------------------------------------------------------------------------


class TestParseVitestJson:
    def test_all_pass(self):
        data = {
            "numTotalTests": 50,
            "numPassedTests": 50,
            "numFailedTests": 0,
            "testResults": [
                {
                    "name": "src/tests/App.test.tsx",
                    "startTime": 1000,
                    "endTime": 3500,
                    "assertionResults": [
                        {"status": "passed", "title": "renders"},
                        {"status": "passed", "title": "handles click"},
                    ],
                }
            ],
        }
        result = parse_vitest_json(json.dumps(data))
        assert result["total"] == 50
        assert result["passed"] == 50
        assert result["failed"] == 0
        assert result["duration_s"] == 2.5
        assert result["failures"] == []

    def test_with_failures(self):
        data = {
            "numTotalTests": 10,
            "numPassedTests": 8,
            "numFailedTests": 2,
            "testResults": [
                {
                    "name": "src/tests/Login.test.tsx",
                    "startTime": 1000,
                    "endTime": 2000,
                    "assertionResults": [
                        {"status": "passed", "title": "renders form"},
                        {
                            "status": "failed",
                            "fullName": "Login > validates email",
                            "title": "validates email",
                            "failureMessages": ["Expected true, got false"],
                        },
                    ],
                },
                {
                    "name": "src/tests/API.test.tsx",
                    "startTime": 2000,
                    "endTime": 3000,
                    "assertionResults": [
                        {
                            "status": "failed",
                            "fullName": "API > fetch returns data",
                            "title": "fetch returns data",
                            "failureMessages": ["TypeError: fetch is not defined"],
                        },
                    ],
                },
            ],
        }
        result = parse_vitest_json(json.dumps(data))
        assert result["total"] == 10
        assert result["passed"] == 8
        assert result["failed"] == 2
        assert result["duration_s"] == 2.0
        assert len(result["failures"]) == 2
        assert result["failures"][0]["test_name"] == "Login > validates email"
        assert "true" in result["failures"][0]["error"]
        assert result["failures"][1]["test_file"] == "src/tests/API.test.tsx"

    def test_invalid_json_returns_error(self):
        result = parse_vitest_json("not json at all {{{")
        assert result["total"] == 0
        assert result["passed"] == 0
        assert "error" in result

    def test_empty_results(self):
        data = {
            "numTotalTests": 0,
            "numPassedTests": 0,
            "numFailedTests": 0,
            "testResults": [],
        }
        result = parse_vitest_json(json.dumps(data))
        assert result["total"] == 0
        assert result["failures"] == []

    def test_missing_fullname_uses_title(self):
        data = {
            "numTotalTests": 1,
            "numPassedTests": 0,
            "numFailedTests": 1,
            "testResults": [
                {
                    "name": "test.tsx",
                    "startTime": 0,
                    "endTime": 100,
                    "assertionResults": [
                        {
                            "status": "failed",
                            "title": "my test",
                            "failureMessages": ["boom"],
                        }
                    ],
                }
            ],
        }
        result = parse_vitest_json(json.dumps(data))
        assert result["failures"][0]["test_name"] == "my test"


# ---------------------------------------------------------------------------
# parse_pytest_output
# ---------------------------------------------------------------------------


class TestParsePytestOutput:
    def test_all_pass(self):
        raw = "430 passed in 12.34s\n"
        result = parse_pytest_output(raw)
        assert result["total"] == 430
        assert result["passed"] == 430
        assert result["failed"] == 0
        assert result["duration_s"] == 12.34
        assert result["failures"] == []

    def test_with_failures(self):
        raw = (
            "FAILED tests/test_auth.py::test_login_invalid\n"
            "FAILED tests/test_db.py::test_migrate\n"
            "tests/test_auth.py:15: AssertionError: expected 401\n"
            "tests/test_db.py:42: RuntimeError: migration failed\n"
            "2 failed, 428 passed in 15.67s\n"
        )
        result = parse_pytest_output(raw)
        assert result["total"] == 430
        assert result["passed"] == 428
        assert result["failed"] == 2
        assert result["duration_s"] == 15.67
        assert len(result["failures"]) == 2
        assert result["failures"][0]["test_file"] == "tests/test_auth.py"
        assert result["failures"][0]["error"] == "AssertionError: expected 401"
        assert result["failures"][1]["test_name"] == "tests/test_db.py:42"

    def test_with_warnings(self):
        raw = "5 failed, 425 passed, 2 warnings in 10.00s\n"
        result = parse_pytest_output(raw)
        assert result["total"] == 430
        assert result["passed"] == 425
        assert result["failed"] == 5
        assert result["duration_s"] == 10.0

    def test_empty_output(self):
        result = parse_pytest_output("")
        assert result["total"] == 0
        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["failures"] == []

    def test_only_passed(self):
        raw = "50 passed in 2.50s\n"
        result = parse_pytest_output(raw)
        assert result["total"] == 50
        assert result["passed"] == 50
        assert result["failed"] == 0


# ---------------------------------------------------------------------------
# run_project_tests
# ---------------------------------------------------------------------------


class TestRunProjectTests:
    @pytest.mark.asyncio
    async def test_unknown_project_raises(self):
        with pytest.raises(ValueError, match="Unknown project"):
            await run_project_tests("nonexistent")

    @pytest.mark.asyncio
    async def test_remote_windows_returns_skip(self, monkeypatch):
        # The remote_windows skip path is preserved in code for future use,
        # even though no project currently uses it. Inject a synthetic entry.
        monkeypatch.setitem(
            PROJECT_REGISTRY,
            "_synthetic_remote",
            {
                "path": "C:/dummy",
                "test_cmd": "irrelevant",
                "env": "remote_windows",
                "framework": "vitest",
            },
        )
        result = await run_project_tests("_synthetic_remote")
        assert result["skipped"] is True
        assert result["project"] == "_synthetic_remote"
        assert "remote_windows" in result["skip_reason"]
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_skip_result_has_required_keys(self, monkeypatch):
        monkeypatch.setitem(
            PROJECT_REGISTRY,
            "_synthetic_remote",
            {
                "path": "C:/dummy",
                "test_cmd": "irrelevant",
                "env": "remote_windows",
                "framework": "vitest",
            },
        )
        result = await run_project_tests("_synthetic_remote")
        required = {"project", "total", "passed", "failed", "duration_s", "failures", "skipped", "skip_reason"}
        assert required.issubset(set(result.keys()))


# ---------------------------------------------------------------------------
# Timeout -> process-group kill (surer P2 + Codex P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_communicate_or_kill_kills_group_on_timeout():
    """Timeout'ta TUM process-grubu (shell + cocuklar) SIGKILL + reap, orphan yok.

    surer P2: wait_for timeout proc'u oldurmez. Codex P1: create_subprocess_shell
    ara-shell calistirir, proc.kill() yalniz onu oldurur -> start_new_session + killpg
    ile cocuklar da olur. Burada shell 2 cocuk sleep spawn eder; grup-kill hepsini alir.
    """
    import asyncio
    import os

    from app.core.ci_runner import _communicate_or_kill

    proc = await asyncio.create_subprocess_shell(
        "sleep 30 & sleep 30",  # shell + 2 cocuk -> grup-kill testi
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    with pytest.raises(TimeoutError):
        await _communicate_or_kill(proc, 1)  # 1sn timeout, sleep 30 -> timeout
    assert proc.returncode is not None  # reaped (zombie kalmadi)
    await asyncio.sleep(0.2)  # kernel grubu temizlesin
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)  # grup tamamen oldu (hicbir uye kalmadi)


@pytest.mark.asyncio
async def test_communicate_or_kill_fallback_when_killpg_fails(monkeypatch):
    """killpg basarisizsa (setsid yok / izin) proc.kill() fallback'i devreye girer (Codex P1 dal)."""
    import asyncio

    from app.core import ci_runner

    # 'exec sleep' -> shell sleep'e DONUSUR (fork yok) -> proc.kill() dogrudan onu oldurur,
    # orphan-cocuk + acik-pipe kalmaz (aksi halde transport cleanup 30sn bloklardi). Bu test
    # yalnizca fallback DALINI (killpg-fail -> proc.kill) kapsar; grup-kill testi ayri.
    proc = await asyncio.create_subprocess_shell(
        "exec sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    def _boom(*_a, **_k):
        raise ProcessLookupError  # grup-kill yok -> fallback yolu

    monkeypatch.setattr(ci_runner.os, "killpg", _boom)
    with pytest.raises(TimeoutError):
        await ci_runner._communicate_or_kill(proc, 1)
    assert proc.returncode is not None  # proc.kill() fallback ile reaped (orphan yok)


@pytest.mark.asyncio
async def test_run_local_happy_path(tmp_path):
    """_run_local gerçek subprocess (start_new_session dahil) -> stdout/rc döner."""
    from app.core.ci_runner import _run_local

    out, err, rc = await _run_local({"test_cmd": "echo hello-ci", "path": str(tmp_path)})
    assert "hello-ci" in out
    assert rc == 0
