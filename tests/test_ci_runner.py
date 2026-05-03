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
    def test_has_all_eight_projects(self):
        expected = {
            "panola",
            "kuafor-panel",
            "kuafor-worker",
            "petvet",
            "renderhane",
            "bilge-arena",
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

    def test_windows_projects_are_remote_windows(self):
        windows_projects = [
            "panola",
            "kuafor-panel",
            "kuafor-worker",
            "petvet",
            "renderhane",
            "bilge-arena",
        ]
        for name in windows_projects:
            assert PROJECT_REGISTRY[name]["env"] == "remote_windows"


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
    async def test_remote_windows_returns_skip(self):
        result = await run_project_tests("panola")
        assert result["skipped"] is True
        assert result["project"] == "panola"
        assert "remote_windows" in result["skip_reason"]
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_all_windows_projects_skip(self):
        windows = ["panola", "kuafor-panel", "kuafor-worker", "petvet", "renderhane", "bilge-arena"]
        for name in windows:
            result = await run_project_tests(name)
            assert result["skipped"] is True, f"{name} should be skipped"
            assert result["project"] == name

    @pytest.mark.asyncio
    async def test_skip_result_has_required_keys(self):
        result = await run_project_tests("petvet")
        required = {"project", "total", "passed", "failed", "duration_s", "failures", "skipped", "skip_reason"}
        assert required.issubset(set(result.keys()))
